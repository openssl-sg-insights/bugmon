#!/usr/bin/env python
# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 2.0
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# The Original Code is ADBFuzz.
#
# The Initial Developer of the Original Code is Christian Holler (decoder).
#
# Contributors:
#  Christian Holler <decoder@mozilla.com> (Original Developer)
#
# ***** END LICENSE BLOCK *****

import base64
import binascii
import io
import json
import logging
import os
import platform
import re
import zipfile
from datetime import datetime as dt
from datetime import timedelta
from enum import Enum

import requests
from autobisect.bisect import BisectionResult, Bisector
from autobisect.build_manager import BuildManager
from autobisect.evaluator import BrowserEvaluator, JSEvaluator
from fuzzfetch import BuildFlags, Fetcher, FetcherException
from fuzzfetch.fetch import Platform

log = logging.getLogger("bugmon")

AVAILABLE_BRANCHES = ["mozilla-central", "mozilla-beta", "mozilla-release"]
HTTP_SESSION = requests.Session()


def _get_url(url):
    """
    Retrieve requested URL
    """
    data = HTTP_SESSION.get(url, stream=True)
    data.raise_for_status()
    return data


def enum(*sequential, **named):
    enums = dict(list(zip(sequential, list(range(len(sequential))))), **named)
    return type("Enum", (), enums)


class RequestedActions(Enum):
    VERIFY_FIXED = 1
    CONFIRM_OPEN = 2
    BISECT = 3


class BugException(Exception):
    pass


class ReproductionResult(object):
    PASSED = 0
    CRASHED = 1
    FAILED = 2
    NO_BUILD = 3

    def __init__(self, status, build_str=None):
        self.status = status
        self.build_str = build_str


class BugMonitor:
    def __init__(self, bugsy, bug_num, working_dir, dry_run=False):
        """

        :param bugsy: Bugsy instance used for retrieving bugs
        :param bug_num: Bug number to analyze
        :param working_dir: Path to working directory
        :param dry_run: Boolean indicating if changes should be made to the bug
        """
        self.bugsy = bugsy
        self.bug = self.bugsy.get(bug_num, "_default")
        self.working_dir = working_dir
        self.dry_run = dry_run
        self.queue = []

        # Initialize placeholders
        self._branch = None
        self._branches = None
        self._build_flags = None
        self._comment_zero = None
        self._initial_build_id = None
        self._platform = None
        self._close_bug = False

        self.testcase = None
        self.fetch_attachments()
        if self.testcase is None:
            raise BugException("Failed to identify testcase!")

        # Determine what type of bug we're evaluating
        if self.bug.component.startswith("JavaScript"):
            self.target = "js"
            self.evaluator = JSEvaluator(self.testcase, flags=self.runtime_opts)
        else:
            self.target = "firefox"
            self.evaluator = BrowserEvaluator(
                self.testcase, env=self.env_vars, prefs=self.identify_prefs()
            )

        self.build_manager = BuildManager()

        # Identify mozilla-central version number
        milestone = _get_url(
            "https://hg.mozilla.org/mozilla-central/raw-file/tip/config/milestone.txt"
        )
        version = milestone.text.splitlines()[-1]
        self.central_version = int(version.split(".", 1)[0])

    @property
    def version(self):
        match = re.match(r"\d+", self.bug.version)
        if match:
            return match.group(0)

        return self.central_version

    @property
    def branch(self):
        """
        Attempt to enumerate the branch the bug was filed against
        """
        if self._branch is None:
            for alias, actual in self.branches.items():
                if self.version == actual:
                    self._branch = alias
                    break

        return self._branch

    @property
    def branches(self):
        """
        Create map of fuzzfetch branch aliases and bugzilla version tags
        :return:
        """
        if self._branches is None:
            self._branches = {
                "central": self.central_version,
                "beta": self.central_version - 1,
                "release": self.central_version - 2,
            }

            for alias in ["esr-next", "esr-stable"]:
                try:
                    rel_num = Fetcher.resolve_esr(alias)
                    if rel_num is not None:
                        self._branches[rel_num] = rel_num
                except FetcherException:
                    pass

        return self._branches

    @property
    def build_flags(self):
        """
        Attempt to enumerate build type based on flags listed in comment 0
        """
        if self._build_flags is None:
            asan = (
                "AddressSanitizer: " in self.comment_zero
                or "--enable-address-sanitizer" in self.comment_zero
            )
            tsan = (
                "ThreadSanitizer: " in self.comment_zero
                or "--enable-thread-sanitizer" in self.comment_zero
            )
            debug = (
                "--enable-debug" in self.comment_zero
                or "assertion" in self.bug.keywords
            )
            fuzzing = "--enable-fuzzing" in self.comment_zero
            coverage = "--enable-coverage" in self.comment_zero
            valgrind = False  # Ignore valgrind for now
            self._build_flags = BuildFlags(
                asan, tsan, debug, fuzzing, coverage, valgrind
            )

        return self._build_flags

    @property
    def comment_zero(self):
        """
        Helper function for retrieving comment zero
        """
        if self._comment_zero is None:
            comments = self.bug.get_comments()
            self._comment_zero = comments[0].text

        return self._comment_zero

    @property
    def env_vars(self):
        """
        Attempt to enumerate any env_variables required
        """
        variables = {}
        tokens = self.comment_zero.split(" ")
        for token in tokens:
            if token.startswith("`") and token.endswith("`"):
                token = token[1:-1]
            if re.match(r"([a-z0-9_]+=[a-z0-9])", token, re.IGNORECASE):
                name, value = token.split("=")
                variables[name] = value

        return variables

    @property
    def initial_build_id(self):
        """
        Attempt to enumerate the original rev specified in comment 0 or bugmon origRev command
        """
        if self._initial_build_id is None:
            if "origRev" in self.commands and re.match(
                "^([a-f0-9]{12}|[a-f0-9]{40})$", self.commands["origRev"]
            ):
                self._initial_build_id = ["origRev"]
            else:
                tokens = self.comment_zero.split(" ")
                for token in tokens:
                    if token.startswith("`") and token.endswith("`"):
                        token = token[1:-1]

                    if re.match(r"^([a-f0-9]{12}|[a-f0-9]{40})$", token, re.IGNORECASE):
                        # Match 12 or 40 character revs
                        self._initial_build_id = token
                        break
                    elif re.match(r"^([0-9]{8}-)([a-f0-9]{12})$", token, re.IGNORECASE):
                        # Match fuzzfetch build identifiers
                        self._initial_build_id = token.split("-")[1]
                        break
                else:
                    # If original rev isn't specified, use the date the bug was created
                    self._initial_build_id = self.bug.creation_time.split("T")[0]

        return self._initial_build_id

    @property
    def platform(self):
        """
        Attempt to enumerate the target platform
        :return:
        """
        if self._platform is None:
            os_ = platform.system()
            if "Linux" in self.bug.op_sys:
                os_ = "Linux"
            elif "Windows" in self.bug.op_sys:
                os_ = "Windows"
            elif "Mac OS" in self.bug.op_sys:
                os_ = "Darwin"

            if os_ != platform.system():
                raise BugException("Cannot process non-native bug (%s)" % os_)

            arch = platform.machine()
            if self.bug.platform == "ARM":
                arch = "ARM64"
            elif self.bug.platform == "x86":
                arch = "i686"
            elif self.bug.platform == "x86_64":
                arch = "AMD64"

            self._platform = Platform(os_, arch)

        return self._platform

    @property
    def runtime_opts(self):
        """
        Attempt to enumerate the runtime flags specified in comment 0
        """
        all_flags = JSEvaluator.get_valid_flags("tip")
        flags = []
        for flag in all_flags:
            if flag in self.comment_zero:
                match = re.search(
                    rf"(--{flag}[a-z0-9=-]*)", self.comment_zero, re.IGNORECASE
                )
                if match is not None:
                    flags.append(match.group(0))

        return flags

    @property
    def commands(self):
        """
        Attempt to extract commands from whiteboard
        """
        commands = {}
        if self.bug.whiteboard:
            match = re.search(r"(?<=\[bugmon:).[^\]]*", self.bug.whiteboard)
            if match is not None:
                for command in match.group(0).split(","):
                    if "=" in command:
                        name, value = command.split("=")
                        commands[name] = value
                    else:
                        commands[command] = None

        return commands

    @commands.setter
    def commands(self, value):
        parts = ",".join([f"{k}={v}" if v is not None else k for k, v in value.items()])
        if len(parts):
            if re.search(r"(?<=\[bugmon:)(.[^\]]*)", self.bug.whiteboard):
                if len(self.commands.keys()):
                    self.bug.whiteboard = re.sub(
                        r"(?<=\[bugmon:)(.[^\]]*)", parts, self.bug.whiteboard
                    )
                else:
                    self.bug.whiteboard = re.sub(
                        r"([bugmon:.[^\]]*)", "", self.bug.whiteboard
                    )
            else:
                self.bug.whiteboard += f"[bugmon:{parts}]"
        else:
            self.bug.whiteboard = re.sub(
                r"(?<=\[bugmon:)(.[^\]]*)", parts, self.bug.whiteboard
            )

    def add_command(self, key, value=None):
        """
        Add a bugmon command to the whiteboard
        :return:
        """
        commands = self.commands
        commands[key] = value
        self.commands = commands

    def remove_command(self, key):
        """
        Remove a bugmon command to the whiteboard
        :return:
        """
        commands = self.commands
        if key in commands:
            del commands[key]

        self.commands = commands

    def fetch_attachments(self):
        """
        Download all attachments and store them in self.working_dir
        """
        attachments = list(
            filter(lambda a: not a.is_obsolete, self.bug.get_attachments())
        )
        for attachment in sorted(attachments, key=lambda a: a.creation_time):
            try:
                data = base64.decodebytes(attachment.data.encode("utf-8"))
            except binascii.Error as e:
                log.warning("Failed to decode attachment: ", e)
                continue

            if attachment.file_name.endswith(".zip"):
                try:
                    z = zipfile.ZipFile(io.BytesIO(data))
                except zipfile.BadZipFile as e:
                    log.warning("Failed to decompress attachment: ", e)
                    continue

                for filename in z.namelist():
                    if os.path.exists(filename):
                        log.warning("Duplicate filename identified: ", filename)
                    z.extract(filename, self.working_dir)
                    if filename.lower().startswith("testcase"):
                        if self.testcase is not None:
                            raise BugException("Multiple testcases identified!")
                        self.testcase = os.path.join(self.working_dir, filename)
            else:
                with open(
                    os.path.join(self.working_dir, attachment.file_name), "wb"
                ) as file:
                    file.write(data)
                    r = re.compile(r"^testcase.*$", re.IGNORECASE)
                    if list(
                        filter(r.match, [attachment.file_name, attachment.description])
                    ):
                        if self.testcase is not None:
                            raise BugException("Multiple testcases identified!")
                        self.testcase = file.name

    def identify_prefs(self):
        """
        Identify prefs in working_dir
        """
        prefs_path = None
        for filename in os.listdir(self.working_dir):
            with open(os.path.join(self.working_dir, filename)) as f:
                if filename.endswith(".js") and "user_pref" in f.read():
                    prefs_path = os.path.join(self.working_dir, filename)
        return prefs_path

    def _needs_bisect(self):
        """
        Helper function to determine eligibility for 'bisect'
        """
        if "bisected" in self.commands:
            return False
        elif "bisect" in self.commands:
            return True

        return False

    def _needs_confirm(self):
        """
        Helper function to determine eligibility for 'confirm'
        """
        if "confirmed" in self.commands:
            return False
        elif "confirm" in self.commands:
            return True
        elif self.bug.status in ["ASSIGNED", "NEW", "UNCONFIRMED", "REOPENED"]:
            return True

        return False

    def _needs_verify(self):
        """
        Helper function to determine eligibility for 'verify'
        """
        if "verified" in self.commands:
            return False
        if "verify" in self.commands:
            return True
        if self.bug.status == "RESOLVED" and self.bug.resolution == "FIXED":
            return True

        return False

    def _confirm_open(self, baseline):
        """
        Attempt to confirm open test cases
        :param baseline: A reproduction result
        """
        if baseline.status == ReproductionResult.CRASHED:
            if "confirmed" not in self.commands:
                self.report(f"Verified bug as reproducible on {baseline.build_str}.")
                self._bisect(find_fix=False)
            else:
                change = dt.strptime(self.bug.last_change_time, "%Y-%m-%dT%H:%M:%SZ")
                if dt.now() - timedelta(days=30) > change:
                    self.report(f"Bug remains reproducible on {baseline.build_str}")
        elif baseline.status == ReproductionResult.PASSED:
            orig = self.reproduce_bug(self.branch, self.initial_build_id)
            if orig.status == ReproductionResult.CRASHED:
                log.info(f"Testcase crashes using the initial build ({orig.build_str})")
                self._bisect(find_fix=True)
            else:
                self.report(
                    f"Unable to reproduce bug using the following builds:",
                    f"> {baseline.build_str}",
                    f"> {orig.build_str}",
                )

            # Remove from further analysis
            self._close_bug = True

        # Set confirmed status and remove the confirm command
        self.add_command("confirmed")
        if "confirm" in self.commands:
            self.remove_command("confirm")

    def _verify_fixed(self, baseline):
        """
        Attempt to verify the bug state

        Bugs marked as resolved and fixed are verified to ensure that they are in fact, fixed
        All other bugs will be tested to determine if the bug still reproduces

        """
        build_str = baseline.build_str
        if baseline.status == ReproductionResult.PASSED:
            initial = self.reproduce_bug(self.branch, self.initial_build_id)
            if initial.status != ReproductionResult.CRASHED:
                self.report(
                    f"Bug appears to be fixed on rev {build_str} but "
                    f"BugMon was unable to reproduce using {self.initial_build_id}."
                )
            else:
                self.report(f"Verified bug as fixed on rev {build_str}.")
                self.bug.status = "VERIFIED"

            # Remove from further analysis
            self._close_bug = True
        elif baseline.status == ReproductionResult.CRASHED:
            self.report(f"Bug is marked as FIXED but still reproduces on {build_str}.")

        for alias, rel_num in self.branches.items():
            if isinstance(rel_num, int):
                flag = f"cf_status_firefox{rel_num}"
            else:
                flag = f"cf_status_firefox_{rel_num}"

            # Only check branches if bug is marked as fixed
            if getattr(self.bug, flag) == "fixed":
                baseline = self.reproduce_bug(alias)
                if baseline.status == ReproductionResult.PASSED:
                    log.info(f"Verified fixed on {flag}")
                    setattr(self.bug, flag, "verified")
                elif baseline.status == ReproductionResult.CRASHED:
                    log.info(f"Bug remains vulnerable on {flag}")
                    setattr(self.bug, flag, "affected")

    def _bisect(self, find_fix):
        """
        Attempt to enumerate the changeset that introduced the bug or,
        if find_fix=True, the changeset that fixed it.

        :param find_fix: Boolean to indicate whether to search for a bug or fix
        """
        if not find_fix:
            start = None
            end = self.initial_build_id
        else:
            start = self.initial_build_id
            end = "latest"

        bisector = Bisector(
            self.evaluator,
            self.target,
            self.branch,
            start,
            end,
            self.build_flags,
            self.platform,
            find_fix,
        )
        result = bisector.bisect()

        # Set bisected status and remove the bisect command
        self.add_command("bisected")
        if "bisect" in self.commands:
            self.remove_command("bisect")

        if result.status != BisectionResult.SUCCESS:
            output = [
                f"Failed to bisect testcase ({result.message}):",
                f"> Start: {result.start.changeset} ({result.start.build_id})",
                f"> End: {result.end.changeset} ({result.end.build_id})",
                f"> BuildFlags: {str(self.build_flags)}",
            ]
            self.report(*output)
        else:
            output = [
                f"> Start: {result.start.changeset} ({result.start.build_id})",
                f"> End: {result.end.changeset} ({result.end.build_id})",
                f"> Pushlog: {result.pushlog}",
            ]

            verb = "fixed" if find_fix else "introduced"
            self.report(
                f"The bug appears to have been {verb} in the following build range:",
                *output,
            )

    def process(self):
        """
        Process bugmon commands present in whiteboard

        Available commands:
        verify - Attempt to verify the bug state
        bisect - Attempt to bisect the bug regression or, if RESOLVED, the bug fix
        """
        if self.branch is None:
            self.report([f"Bug filed against non-supported branch ({self.version})"])
            self._close_bug = True
            self.update()
            return

        actions = []
        if self._needs_verify():
            actions.append(RequestedActions.VERIFY_FIXED)

        if self._needs_confirm():
            actions.append(RequestedActions.CONFIRM_OPEN)

        if self._needs_bisect():
            actions.append(RequestedActions.BISECT)

        if not len(actions):
            log.info(f"Nothing to do for bug {self.bug.id}")
            return

        baseline = self.reproduce_bug(self.branch)
        if baseline.status == ReproductionResult.NO_BUILD:
            log.warning(f"Could not find matching build to verify status")
            return
        if baseline.status == ReproductionResult.FAILED:
            log.warning(f"Unable to verify status due to bad build")
            return

        # Some testcases require setting the cwd to the parent dir
        previous_path = os.getcwd()
        os.chdir(self.working_dir)
        try:
            baseline = self.reproduce_bug(self.branch)
            if baseline.status == ReproductionResult.NO_BUILD:
                log.warning(f"Could not find matching build to verify status")
                return
            if baseline.status == ReproductionResult.FAILED:
                log.warning(f"Unable to verify status due to bad build")
                return

            for action in actions:
                if action == RequestedActions.VERIFY_FIXED:
                    self._verify_fixed(baseline)
                elif action == RequestedActions.CONFIRM_OPEN:
                    self._confirm_open(baseline)
                elif action == RequestedActions.BISECT:
                    self._bisect(baseline.status == ReproductionResult.PASSED)
        finally:
            os.chdir(previous_path)

        # Post updates and comments
        self.update()

    def reproduce_bug(self, branch, build_id=None):
        try:
            direction = Fetcher.BUILD_ORDER_ASC
            if build_id is None:
                build_id = "latest"
                direction = None

            build = Fetcher(
                self.target,
                branch,
                build_id,
                self.build_flags,
                self.platform,
                nearest=direction,
            )
        except FetcherException as e:
            log.error(f"Error fetching build: {e}")
            return ReproductionResult(ReproductionResult.NO_BUILD)

        log.info(
            f"Attempting to reproduce bug on mozilla-{branch} {build.build_id}-{build.changeset[:12]}"
        )
        with self.build_manager.get_build(build) as build_path:
            status = self.evaluator.evaluate_testcase(build_path)
            build_str = f"mozilla-{self.branch} {build.build_id}-{build.changeset[:12]}"
            if status == Bisector.BUILD_CRASHED:
                return ReproductionResult(ReproductionResult.CRASHED, build_str)
            elif status == Bisector.BUILD_PASSED:
                return ReproductionResult(ReproductionResult.PASSED, build_str)
            else:
                return ReproductionResult(ReproductionResult.FAILED, build_str)

    def report(self, *messages):
        """
        Push changes or if dry_run, report to log
        :param messages: List of comments
        :return:
        """
        for message in messages:
            self.queue.append(message)
            for line in message.splitlines():
                log.info(line)

    def update(self):
        """
        Post any changes to the bug
        """
        if self._close_bug:
            if "bugmon" in self.bug.keywords:
                self.bug.keywords.remove("bugmon")
                self.report(
                    "Removing bugmon keyword as no further action possible.",
                    "Please review the bug and re-add the keyword for further analysis.",
                )

        diff = self.bug.diff()
        if diff:
            log.info(f"Changes: {json.dumps(diff)}")
            if not self.dry_run:
                self.bugsy.put(self.bug)
                self.bug.update()

        if not self.dry_run:
            self.bug.add_comment("Bugmon Analysis:\n%s" % "\n".join(self.queue))
            self.queue = []

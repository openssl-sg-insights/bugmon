# -*- coding: utf-8 -*-

# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at http://mozilla.org/MPL/2.0/.

import base64
import binascii
import copy
import io
import json
import logging
import os
import re
import zipfile
from datetime import datetime as dt
from datetime import timedelta
from pathlib import Path

from autobisect import EvaluatorResult
from autobisect.bisect import BisectionResult, Bisector
from autobisect.build_manager import BuildManager
from autobisect.evaluators import BrowserEvaluator, JSEvaluator
from fuzzfetch import BuildSearchOrder, Fetcher, FetcherException

log = logging.getLogger("bugmon")

AVAILABLE_BRANCHES = ["mozilla-central", "mozilla-beta", "mozilla-release"]

TESTCASE_URL = "https://github.com/MozillaSecurity/bugmon#testcase-identification"


class BugException(Exception):
    """
    Exception for Bugmon related issues
    """


class ReproductionResult:
    """
    Class for storing reproduction results
    """

    def __init__(self, status, build_str=None):
        self.status = status
        self.build_str = build_str


class BugMonitor:
    """
    Main bugmon class
    """

    def __init__(self, bugsy, bug, working_dir, dry_run=False):
        """

        :param bugsy: Bugsy instance used for retrieving bugs
        :param bug: Bug to analyze
        :param working_dir: Path to working directory
        :param dry_run: Boolean indicating if changes should be made to the bug
        """
        self.bugsy = bugsy
        self.bug = bug
        self.working_dir = working_dir
        self.dry_run = dry_run
        self.queue = []
        self.results = {}

        self._close_bug = False
        self._testcase = None

        self.target = None
        self.evaluator = None
        self.build_manager = BuildManager()

    @property
    def prefs(self):
        """
        Identify prefs in working_dir
        """
        prefs_path = None
        for filename in self.working_dir.glob("*.js"):
            file_path = self.working_dir / filename
            if "user_pref" in file_path.read_text():
                prefs_path = file_path
                break
        return prefs_path

    def _bisect(self):
        """
        Attempt to enumerate the changeset that introduced or fixed the bug
        """
        tip = self._reproduce_bug(self.bug.branch)
        if tip.status == EvaluatorResult.BUILD_FAILED:
            log.warning("Failed to bisect bug (bad build)")
            return

        # If tip doesn't crash, bisect the fix
        find_fix = tip.status != EvaluatorResult.BUILD_CRASHED
        if find_fix:
            start = self.bug.initial_build_id
            end = "latest"
        else:
            start = None
            end = self.bug.initial_build_id

        bisector = Bisector(
            self.evaluator,
            self.target,
            self.bug.branch,
            start,
            end,
            self.bug.build_flags,
            self.bug.platform,
            find_fix,
        )
        result = bisector.bisect()

        # Set bisected status and remove the bisect command
        self.add_command("bisected")
        if "bisect" in self.bug.commands:
            self.remove_command("bisect")

        if result.status != BisectionResult.SUCCESS:
            output = [
                f"Failed to bisect testcase ({result.message}):",
                f"> Start: {result.start.changeset} ({result.start.id})",
                f"> End: {result.end.changeset} ({result.end.id})",
                f"> BuildFlags: {str(self.bug.build_flags)}",
            ]
            self.report(*output)
        else:
            output = [
                f"> Start: {result.start.changeset} ({result.start.id})",
                f"> End: {result.end.changeset} ({result.end.id})",
                f"> Pushlog: {result.pushlog}",
            ]

            verb = "fixed" if find_fix else "introduced"
            self.report(
                f"The bug appears to have been {verb} in the following build range:",
                *output,
            )

    def _confirm_open(self):
        """
        Attempt to confirm open test cases
        """
        tip = self._reproduce_bug(self.bug.branch)
        if tip.status == EvaluatorResult.BUILD_FAILED:
            log.warning("Failed to confirm bug (bad build)")
            return

        if tip.status == EvaluatorResult.BUILD_CRASHED:
            if "confirmed" not in self.bug.commands:
                self.report(f"Verified bug as reproducible on {tip.build_str}.")
                self._bisect()
            else:
                change = dt.strptime(self.bug.last_change_time, "%Y-%m-%dT%H:%M:%SZ")
                if dt.now() - timedelta(days=30) > change:
                    self.report(f"Bug remains reproducible on {tip.build_str}")
        elif tip.status == EvaluatorResult.BUILD_PASSED:
            orig = self._reproduce_bug(self.bug.branch, self.bug.initial_build_id)
            if orig.status == EvaluatorResult.BUILD_CRASHED:
                log.info(f"Testcase crashes using the initial build ({orig.build_str})")
                self._bisect()
            elif orig.status == EvaluatorResult.BUILD_PASSED:
                self.report(
                    "Unable to reproduce bug using the following builds:",
                    f"> {tip.build_str}",
                    f"> {orig.build_str}",
                )

            # Remove from further analysis
            self._close_bug = True

        # Set confirmed status and remove the confirm command
        self.add_command("confirmed")
        if "confirm" in self.bug.commands:
            self.remove_command("confirm")

    def _verify_fixed(self):
        """
        Attempt to verify the bug state

        Bugs marked as resolved and fixed are verified to ensure that they are in fact, fixed
        All other bugs will be tested to determine if the bug still reproduces

        """
        if self.bug.status != "VERIFIED":
            patch_rev = self.bug.find_patch_rev(self.bug.branch)
            tip = self._reproduce_bug(self.bug.branch, patch_rev)

            build_str = tip.build_str
            if tip.status == EvaluatorResult.BUILD_PASSED:
                initial = self._reproduce_bug(
                    self.bug.branch, self.bug.initial_build_id
                )
                if initial.status != EvaluatorResult.BUILD_CRASHED:
                    self.report(
                        f"Bug appears to be fixed on {build_str} but "
                        f"BugMon was unable to reproduce using {initial.build_str}."
                    )
                    self._close_bug = True
                else:
                    self.report(f"Verified bug as fixed on rev {build_str}.")
                    self.bug.status = "VERIFIED"

            elif tip.status == EvaluatorResult.BUILD_CRASHED:
                self.report(f"Bug marked as FIXED but still reproduces on {build_str}.")
                self.bug.status = "REOPENED"
                self.add_command("confirmed")

        branches_verified = True
        for alias, rel_num in self.bug.branches.items():
            if isinstance(rel_num, int):
                flag = f"cf_status_firefox{rel_num}"
            else:
                flag = f"cf_status_firefox_{rel_num}"

            # Only check branches if bug is marked as fixed
            if getattr(self.bug, flag) == "fixed":
                patch_rev = self.bug.find_patch_rev(alias)
                branch = self._reproduce_bug(alias, patch_rev)
                if branch.status == EvaluatorResult.BUILD_PASSED:
                    log.info(f"Verified fixed on {flag}")
                    setattr(self.bug, flag, "verified")
                    continue

                branches_verified = False
                if branch.status == EvaluatorResult.BUILD_CRASHED:
                    log.info(f"Bug remains vulnerable on {flag}")
                    setattr(self.bug, flag, "affected")

        if self.bug.status == "VERIFIED" and branches_verified:
            # Remove from further analysis
            self._close_bug = True

    def _reproduce_bug(self, branch, bid=None):
        """
        Method for evaluating testcase using the supplied branch and optional build ID
        Caches previous results

        :param branch: Branch where build is found
        :param bid: Build id (rev or date)
        """
        try:
            direction = BuildSearchOrder.ASC
            if bid is None:
                bid = "latest"
                direction = None

            build = Fetcher(
                branch,
                bid,
                self.bug.build_flags,
                self.bug.platform,
                nearest=direction,
            )
        except FetcherException as e:
            log.error(f"Error fetching build: {e}")
            return ReproductionResult(EvaluatorResult.BUILD_FAILED)

        # Check if this branch and build was already tested
        if branch in self.results:
            if build.id in self.results[branch]:
                return self.results[branch][build.id]
        else:
            self.results[branch] = {}

        build_str = f"mozilla-{self.bug.branch} {build.id}-{build.changeset[:12]}"
        log.info(f"Attempting to reproduce bug on {build_str}")

        with self.build_manager.get_build(build, self.target) as build_path:
            status = self.evaluator.evaluate_testcase(build_path)
            self.results[branch][build.id] = ReproductionResult(status, build_str)

            return self.results[branch][build.id]

    def add_command(self, key, value=None):
        """
        Add a bugmon command to the whiteboard
        :return:
        """
        commands = copy.deepcopy(self.bug.commands)
        commands[key] = value
        self.bug.commands = commands

    def remove_command(self, key):
        """
        Remove a bugmon command to the whiteboard
        :return:
        """
        commands = copy.deepcopy(self.bug.commands)
        if key in commands:
            del commands[key]

        self.bug.commands = commands

    def fetch_attachments(self):
        """
        Download all attachments and store them in self.working_dir
        """
        attachments = filter(lambda a: not a.is_obsolete, self.bug.get_attachments())
        for attachment in sorted(attachments, key=lambda a: a.creation_time):
            try:
                data = base64.decodebytes(attachment.data.encode("utf-8"))
            except binascii.Error as e:
                log.warning("Failed to decode attachment: %s", e)
                continue

            if attachment.file_name.endswith(".zip"):
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        for filename in z.namelist():
                            if os.path.exists(filename):
                                log.warning(
                                    "Duplicate filename identified: %s", filename
                                )
                            z.extract(filename, self.working_dir)
                            if filename.lower().startswith("test"):
                                if self._testcase is not None:
                                    raise BugException("Multiple testcases identified!")
                                self._testcase = Path(self.working_dir, filename)
                except zipfile.BadZipFile as e:
                    log.warning("Failed to decompress attachment: %s", e)
                    continue

            else:
                file_path = Path(self.working_dir, attachment.file_name)
                file_path.write_bytes(data)
                r = re.compile(r"^testcase.*$", re.IGNORECASE)
                targets = [attachment.file_name, attachment.description]
                if any(r.match(target) for target in targets):
                    if self._testcase is not None:
                        raise BugException("Multiple testcases identified!")
                    self._testcase = file_path

        return self._testcase

    def needs_bisect(self):
        """
        Helper function to determine eligibility for 'bisect'
        """
        if "bisected" in self.bug.commands:
            return False
        if "bisect" in self.bug.commands:
            return True

        return False

    def needs_confirm(self):
        """
        Helper function to determine eligibility for 'confirm'
        """
        if "confirmed" in self.bug.commands:
            return False
        if "confirm" in self.bug.commands:
            return True
        if self.bug.status in ["ASSIGNED", "NEW", "UNCONFIRMED", "REOPENED"]:
            return True

        return False

    def needs_verify(self):
        """
        Helper function to determine eligibility for 'verify'
        """
        if "verified" in self.bug.commands:
            return False
        if "verify" in self.bug.commands:
            return True
        if self.bug.status == "RESOLVED" and self.bug.resolution == "FIXED":
            return True

        return False

    def is_supported(self):
        """
        Simple checks to determine if bug is valid candidate for Bugmon

        :return: Boolean
        """

        # Check that the branch is available on taskcluster
        if self.bug.branch is None:
            self.report(f"Bug filed against non-supported branch ({self.bug.version})")
            self._close_bug = True
            return False

        if self.bug.resolution in ("DUPLICATE", "INVALID", "WORKSFORME", "WONTFIX"):
            self.report(f"No valid actions for resolution ({self.bug.resolution})")
            self._close_bug = True

        # Check that we can parse the testcase
        if self.fetch_attachments() is None:
            self.report(
                "Failed to identify testcase.  "
                "Please ensure that the testcase meets the requirements identified here: "
                "https://github.com/MozillaSecurity/bugmon#testcase-identification",
            )
            self._close_bug = True
            return False

        return True

    def process(self):
        """
        Process bugmon commands present in whiteboard

        Available commands:
        verify - Attempt to verify the bug state
        bisect - Attempt to bisect the bug regression or, if RESOLVED, the bug fix
        confirm - Attempt to confirm that testcase reproduces
        """
        if not self.is_supported():
            self.commit()
            return

        # Setup the evaluators
        if self.bug.component.lower().startswith("javascript"):
            self.target = "js"
            self.evaluator = JSEvaluator(self._testcase, flags=self.bug.runtime_opts)
        else:
            self.target = "firefox"
            self.evaluator = BrowserEvaluator(
                self._testcase,
                env=self.bug.env,
                prefs=self.prefs,
                repeat=10,
                timeout=60,
            )

        if self.needs_verify():
            self._verify_fixed()
        elif self.needs_confirm():
            self._confirm_open()
        elif self.needs_bisect():
            self._bisect()

        # Post updates and comments
        self.commit()

    def report(self, *messages):
        """
        Output and store messages in queue
        :param messages: List of comments
        :return:
        """
        for message in messages:
            self.queue.append(message)
            for line in message.splitlines():
                log.info(line)

    def commit(self):
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

        if self.queue:
            results = "\n".join(self.queue)
            self.bug.comment = {
                "body": f"Bugmon Analysis:\n{results}",
                "is_private": False,
                "is_markdown": True,
            }
            self.queue = []

        diff = self.bug.diff()
        if diff:
            log.info(f"Changes: {json.dumps(diff)}")
            if not self.dry_run:
                self.bugsy.put(self.bug)
                self.bug.update()

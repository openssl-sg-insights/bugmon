# -*- coding: utf-8 -*-

# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at http://mozilla.org/MPL/2.0/.
# pylint: disable=too-many-public-methods
import json
import platform
import re
from datetime import datetime

import requests
from autobisect import JSEvaluator
from bugsy import Attachment, Bug, Comment
from fuzzfetch import BuildFlags, Fetcher, FetcherException, Platform

from . import BugException
from .utils import HG_BASE, _get_milestone, _get_url

REV_MATCH = r"([a-f0-9]{12}|[a-f0-9]{40})"
BID_MATCH = r"([0-9]{8}-)([a-f0-9]{12})"


def sanitize_bug(obj):
    """
    Helper method for converting Bug to JSON
    :param obj:
    :return:
    """
    if isinstance(obj, datetime):
        return datetime.strftime(obj, "%Y-%m-%dT%H:%M:%SZ")
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, Attachment):
        return obj.to_dict()
    if isinstance(obj, Comment):
        # pylint: disable=protected-access
        return obj._comment

    return obj


class EnhancedBug(Bug):
    """
    Bug wrapper which includes helper methods needed by Bugmon

    :param bugsy: Bugsy instance
    :param kwargs: Bug data
    """

    LOCAL_ATTRS = frozenset(
        {
            "_branch",
            "_branches",
            "_build_flags",
            "_central_version",
            "_comment_zero",
            "_env_variables",
            "_initial_build_id",
            "_platform",
            "commands",
        }
    )

    def __init__(self, bugsy, **kwargs):
        """ Initializes LocalAttachment """
        super().__init__(bugsy, **kwargs)

        if bugsy is None and ("attachments" not in kwargs or "comments" not in kwargs):
            raise BugException("Cannot init Bug without Bugsy instance or cached data")

        # Initialize placeholders
        self._branch = None
        self._branches = None
        self._build_flags = None
        self._central_version = None
        self._comment_zero = None
        self._env_variables = None
        self._initial_build_id = None
        self._platform = None

    def __setattr__(self, attr, value):
        if attr in self.LOCAL_ATTRS:
            object.__setattr__(self, attr, value)
        else:
            super().__setattr__(attr, value)

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
        """
        if self._branches is None:
            self._branches = {
                "central": self.central_version,
                "beta": self.central_version - 1,
                "release": self.central_version - 2,
            }

            for alias in ["esr-next", "esr-stable"]:
                try:
                    release = Fetcher.resolve_esr(alias)
                    if release is not None:
                        version = int(release.strip("esr"))
                        self._branches[release] = version
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
                "AddressSanitizer" in self.comment_zero
                or "--enable-address-sanitizer" in self.comment_zero
            )
            tsan = (
                "ThreadSanitizer" in self.comment_zero
                or "--enable-thread-sanitizer" in self.comment_zero
            )
            debug = (
                "--enable-debug" in self.comment_zero or "assertion" in self.keywords
            )
            fuzzing = "--enable-fuzzing" in self.comment_zero
            coverage = "--enable-coverage" in self.comment_zero
            valgrind = False  # Ignore valgrind for now
            self._build_flags = BuildFlags(
                asan, tsan, debug, fuzzing, coverage, valgrind
            )

        return self._build_flags

    @property
    def central_version(self):
        """
        Return numeric version for tip
        """
        if self._central_version is None:
            self._central_version = _get_milestone()

        return self._central_version

    @property
    def commands(self):
        """
        Attempt to extract commands from whiteboard
        """
        commands = {}
        if self._bug["whiteboard"]:
            match = re.search(r"(?<=\[bugmon:)[^]]+", self._bug["whiteboard"])
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
        if len(parts) != 0:
            if re.search(r"(?<=\[bugmon:)([^]]*)", self._bug["whiteboard"]):
                # Update existing bugmon command list
                pattern = re.compile(r"(?<=\[bugmon:)([^]]*)")
                result = pattern.sub(parts, self._bug["whiteboard"])
            else:
                # Insert new bugmon command list
                result = f"{self._bug['whiteboard']}[bugmon:{parts}]"
        else:
            # Remove bugmon from whiteboard
            pattern = re.compile(r"(\[bugmon:.*?])")
            result = pattern.sub("", self._bug["whiteboard"])

        if result is not None:
            self._bug["whiteboard"] = result

    @property
    def comment_zero(self):
        """
        Helper function for retrieving comment zero
        """
        if self._comment_zero is None:
            comments = self.get_comments()
            self._comment_zero = comments[0].text

        return self._comment_zero

    @property
    def env(self):
        """
        Attempt to enumerate any env_variables required
        """
        if self._env_variables is None:
            self._env_variables = {}
            tokens = self.comment_zero.split(" ")
            for token in tokens:
                if token.startswith("`") and token.endswith("`"):
                    token = token[1:-1]
                if re.match(r"([a-z0-9_]+=[a-z0-9])", token, re.IGNORECASE):
                    name, value = token.split("=")
                    self._env_variables[name] = value

        return self._env_variables

    @property
    def initial_build_id(self):
        """
        Attempt to enumerate the original rev specified in comment 0 or bugmon origRev command
        """
        if self._initial_build_id is None:
            tokens = []
            if re.match(rf"^{REV_MATCH}$", self.commands.get("origRev", "")):
                tokens.append(self.commands["origRev"])
            else:
                tokens.extend(re.findall(r"([A-Za-z0-9_-]+)", self.comment_zero))

            for token in tokens:
                if token.startswith("`") and token.endswith("`"):
                    token = token[1:-1]

                # Match 12 or 40 character revs
                if re.match(rf"^{REV_MATCH}$", token, re.IGNORECASE):
                    try:
                        _get_url(f"{HG_BASE}/{self.branch}/json-rev/{token}")
                        self._initial_build_id = token
                        break
                    except requests.exceptions.HTTPError:
                        pass

                # Match fuzzfetch build identifiers
                if re.match(rf"^{BID_MATCH}$", token, re.IGNORECASE):
                    self._initial_build_id = token.split("-")[1]
                    break
            else:
                # If original rev isn't specified, use the date the bug was created
                self._initial_build_id = self.creation_time.split("T")[0]

        return self._initial_build_id

    @property
    def platform(self):
        """
        Attempt to enumerate the target platform
        """
        if self._platform is None:
            os_ = platform.system()
            if "Linux" in self.op_sys:
                os_ = "Linux"
            elif "Windows" in self.op_sys:
                os_ = "Windows"
            elif "Mac OS" in self.op_sys:
                os_ = "Darwin"

            if os_ != platform.system():
                raise BugException(f"Cannot process non-native bug ({os_})")

            arch = platform.machine()
            if self._bug["platform"] == "ARM":
                arch = "ARM64"
            elif self._bug["platform"] == "x86":
                arch = "i686"
            elif self._bug["platform"] == "x86_64":
                arch = "AMD64"

            self._platform = Platform(os_, arch)

        return self._platform

    @property
    def version(self):
        """
        Attempt to enumerate the version the bug was filed against
        """
        if isinstance(self._bug["version"], int):
            return self._bug["version"]

        return self.central_version

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

    def get_attachments(self):
        """
        Return list of attachments
        """
        if self._bugsy is None:
            attachments = self._bug.get("attachments", [])
            return [LocalAttachment(**a) for a in attachments]

        return super().get_attachments()

    def add_attachment(self, attachment):
        """
        Add a new attachment when a bugsy instance is present

        :param attachment: Attachment
        """
        if self._bugsy is None:
            raise TypeError("Method not supported when using a cached bug")
        super().add_attachment(attachment)

    def get_comments(self):
        """
        Returns list of comments
        Bugs without a bugsy instance are expected to include comments
        """
        if self._bugsy is None:
            comments = self._bug.get("comments", [])
            return [LocalComment(**c) for c in comments]

        return super().get_comments()

    def add_comment(self, comment):
        """
        Add a new comment when a bugsy instance is present

        :param comment: comment
        """
        if self._bugsy is None:
            raise TypeError("Method not supported when using a cached bug")
        super().add_comment(comment)

    def diff(self):
        """
        Overload Bug.diff() to strip attachments and comments
        :return:
        """
        changed = super().diff()

        # These keys should never occur in the diff
        changed.pop("attachments", None)
        changed.pop("comments", None)

        return changed

    def find_patch_rev(self, branch):
        """
        Attempt to determine patch rev for the supplied branch

        :param branch: Branch name
        :return: Patch revision
        """
        alias = f"mozilla-{branch}"
        if branch == "central":
            pattern = re.compile(rf"(?:{HG_BASE}/{alias}/rev/){REV_MATCH}")
        else:
            pattern = re.compile(rf"(?:{HG_BASE}/releases/{alias}/rev/){REV_MATCH}")

        comments = self.get_comments()
        for comment in sorted(comments, key=lambda c: c.creation_time, reverse=True):
            match = pattern.match(comment.text)
            if match:
                return match.group(1)

        return None

    def to_dict(self):
        """
        Bug.to_dict() is used via Bugsy remote methods
        To avoid sending bad data, we need to exclude attachments and comments

        """
        excluded = ["attachments", "comments"]
        return {k: v for k, v in self._bug.items() if k not in excluded}

    def to_json(self):
        """
        Export entire bug in JSON safe format
        May include attachments and comments

        :return:
        """
        return json.dumps(self._bug, default=sanitize_bug)

    def update(self):
        """Update bug when a bugsy instance is present"""
        if self._bugsy is None:
            raise TypeError("Method not supported when using a cached bug")

        super().update()

    @classmethod
    def cache_bug(cls, bug):
        """
        Create a cached instance of EnhancedBug

        :param bug: A EnhancedBug instance with Bugsy
        :type bug: EnhancedBug
        :return: A cached EnhancedBug instance
        :rtype: EnhancedBug
        """
        # pylint: disable=protected-access
        if bug._bugsy is None:
            raise TypeError("Method not supported when using a cached bug")

        bug_data = bug.to_dict()
        attachments = bug.get_attachments()
        bug_data["attachments"] = [a.to_dict() for a in attachments]

        comments = bug.get_comments()
        # pylint: disable=protected-access
        bug_data["comments"] = [c._comment for c in comments]

        return cls(None, **bug_data)


class LocalAttachment(Attachment):
    """
    Class for storing attachments without access to bugzilla

    :param kwargs: Bug data
    """

    def __init__(self, **kwargs):
        """ Initializes LocalAttachment """
        super().__init__(None, **kwargs)

    def update(self):
        """
        Disable update
        """
        raise TypeError("Method not supported when using a cached attachment")


class LocalComment(Comment):
    """
    Class for storing comments without access to bugzilla

    :param kwargs: Comment data
    """

    def __init__(self, **kwargs):
        """ Initializes LocalComment """
        super().__init__(None, **kwargs)

    def add_tags(self, tags):
        """
        Disable add_tags

        :param tags:
        """
        raise TypeError("Method not supported when using a cached comment")

    def remove_tags(self, tags):
        """
        Disable remove_tags

        :param tags:
        """
        raise TypeError("Method not supported when using a cached comment")

    def to_dict(self):
        """
        Return comment content as dict
        """
        return self._comment

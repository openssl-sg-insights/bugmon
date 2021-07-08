# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
from abc import abstractmethod
from pathlib import Path
from typing import Iterator

from autobisect import Evaluator

from bugmon.bug import EnhancedBug


class BaseEvaluatorConfig(Evaluator):
    """Base evaluator configuration class"""

    @classmethod
    @abstractmethod
    def iterate(
        cls, bug: EnhancedBug, working_dir: Path
    ) -> Iterator["BaseEvaluatorConfig"]:
        """Generator for iterating over possible Evaluator configurations
        :param bug: The bug to evaluate
        :param working_dir: Directory containing bug attachments
        :return: Class instance
        """

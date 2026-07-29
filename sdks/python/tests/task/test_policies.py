"""Tests for ergon.task.policies — normalization of env-driven values."""

import pytest

from ergon.task import policies


class TestConsumerLoopPolicyMode:
    def test_default_is_batch(self):
        assert policies.ConsumerLoopPolicy().mode == "batch"

    def test_accepts_continuous(self):
        assert policies.ConsumerLoopPolicy(mode="continuous").mode == "continuous"

    def test_normalizes_case_and_whitespace(self):
        assert policies.ConsumerLoopPolicy(mode="  CONTINUOUS ").mode == "continuous"

    def test_empty_and_none_fall_back_to_batch(self):
        assert policies.ConsumerLoopPolicy(mode="").mode == "batch"
        assert policies.ConsumerLoopPolicy(mode=None).mode == "batch"
        assert policies.ConsumerLoopPolicy(mode="none").mode == "batch"

    def test_rejects_unknown_mode(self):
        with pytest.raises(ValueError):
            policies.ConsumerLoopPolicy(mode="turbo")

#!/usr/bin/env python3
"""Unit tests for sable_batch_key_lib (SABLE-be4lo.1): the single owned
module for (base, member) preview identity keys — pairwise preview_kick_key
(moved here unchanged) and its N-ary generalization, setkey.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import sable_batch_key_lib as batch_key  # noqa: E402

BASE = "a" * 40
M1 = "b" * 40
M2 = "c" * 40
M3 = "d" * 40


# --- setkey: sorted-input identity -------------------------------------------

def test_setkey_identity_across_input_order():
    forward = batch_key.setkey(BASE, [M1, M2, M3])
    shuffled = batch_key.setkey(BASE, [M3, M1, M2])
    reversed_order = batch_key.setkey(BASE, [M3, M2, M1])
    assert forward == shuffled == reversed_order


def test_setkey_does_not_mutate_its_input_list():
    members = [M3, M1, M2]
    batch_key.setkey(BASE, members)
    assert members == [M3, M1, M2]


# --- setkey: sensitivity ------------------------------------------------------

def test_setkey_sensitivity_to_a_changed_member():
    original = batch_key.setkey(BASE, [M1, M2, M3])
    changed = batch_key.setkey(BASE, [M1, M2, "e" * 40])
    assert original != changed


def test_setkey_sensitivity_to_the_base():
    a = batch_key.setkey(BASE, [M1, M2])
    b = batch_key.setkey("f" * 40, [M1, M2])
    assert a != b


# --- setkey: degenerate N=1 equals the pairwise key --------------------------

def test_setkey_degenerate_n1_equals_pairwise_preview_kick_key():
    assert batch_key.setkey(BASE, [M1]) == batch_key.preview_kick_key(BASE, M1)


# --- setkey: non-emptiness guard ----------------------------------------------

def test_setkey_rejects_an_empty_member_list():
    with pytest.raises(ValueError):
        batch_key.setkey(BASE, [])


# --- preview_kick_key: moved unchanged ----------------------------------------

def test_preview_kick_key_is_a_pure_function_of_the_two_parents():
    assert batch_key.preview_kick_key(BASE, M1) == batch_key.preview_kick_key(BASE, M1)
    assert batch_key.preview_kick_key(BASE, M1) != batch_key.preview_kick_key(M1, BASE)


def test_preview_kick_key_rejects_missing_parent():
    with pytest.raises(ValueError):
        batch_key.preview_kick_key("", M1)
    with pytest.raises(ValueError):
        batch_key.preview_kick_key(BASE, "")


# --- pair_parents: the canonical two-parent order -----------------------------

def test_pair_parents_orders_base_first():
    assert batch_key.pair_parents(BASE, M1) == [BASE, M1]


# --- tip_matches: the integrity invariant -------------------------------------

def test_tip_matches_true_on_equal_shas():
    assert batch_key.tip_matches(M1, M1) is True


def test_tip_matches_false_on_differing_shas():
    assert batch_key.tip_matches(M1, M2) is False

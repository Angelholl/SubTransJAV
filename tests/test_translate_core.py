# -*- coding: utf-8 -*-
"""translate.core 上下文预算函数单元测试（P0 #1：token_budget 三参调用）"""
from subtransjav.translate.core import (
    cap_batch_size_for_context,
    compute_max_output_tokens,
)


def test_cap_batch_size_defaults():
    assert cap_batch_size_for_context(30, 8192) == 11
    assert cap_batch_size_for_context(30, 8192, None) == 11


def test_cap_batch_size_accepts_token_budget():
    got = cap_batch_size_for_context(30, 8192, {
        'overhead': 2500, 'tokens_per_line': 500})
    assert got == 11


def test_cap_batch_size_custom_budget():
    got = cap_batch_size_for_context(30, 8192, {
        'overhead': 1000, 'tokens_per_line': 100})
    assert got == 30


def test_compute_max_output_tokens_accepts_token_budget():
    base = compute_max_output_tokens(11, 8192)
    assert base == compute_max_output_tokens(11, 8192, None)
    got = compute_max_output_tokens(11, 8192, {
        'overhead': 2500, 'input_per_line_cjk': 300,
        'output_per_line_en': 120, 'output_fixed_tags': 500})
    assert got == base > 0

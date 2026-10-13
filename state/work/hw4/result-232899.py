import pytest
from solution import merge_intervals

def test_empty_input_returns_empty_list():
    assert merge_intervals([]) == []

def test_single_interval_is_returned_unchanged():
    assert merge_intervals([[4, 9]]) == [[4, 9]]

def test_disjoint_intervals_stay_separate_and_sorted():
    assert merge_intervals([[5, 6], [1, 2]]) == [[1, 2], [5, 6]]

def test_overlapping_intervals_merge():
    assert merge_intervals([[1, 4], [3, 7]]) == [[1, 7]]

def test_touching_intervals_merge_when_start_equals_previous_end():
    assert merge_intervals([[1, 3], [3, 5]]) == [[1, 5]]

def test_unsorted_input_is_sorted_before_merging():
    assert merge_intervals([[8, 10], [1, 3], [2, 6], [15, 18]]) == [
        [1, 6],
        [8, 10],
        [15, 18],
    ]

def test_nested_interval_keeps_maximum_end():
    assert merge_intervals([[1, 10], [3, 4]]) == [[1, 10]]
    assert merge_intervals([[1, 10], [2, 5], [4, 12]]) == [[1, 12]]

def test_negative_and_boundary_coordinates_merge_correctly():
    assert merge_intervals([[-100, -50], [-60, 0], [100, 100]]) == [
        [-100, 0],
        [100, 100],
    ]

def test_zero_width_intervals_are_preserved_and_merged_when_equal():
    assert merge_intervals([[0, 0], [5, 5], [5, 5]]) == [[0, 0], [5, 5]]

def test_input_list_is_not_mutated():
    data = [[3, 4], [1, 2], [2, 3]]
    snapshot = [pair[:] for pair in data]
    result = merge_intervals(data)
    assert result == [[1, 4]]
    assert data == snapshot

def test_reversed_bounds_raise_value_error():
    with pytest.raises(ValueError):
        merge_intervals([[2, 1]])
    with pytest.raises(ValueError):
        merge_intervals([[1, 2], [9, 3]])

def test_boolean_values_are_rejected():
    with pytest.raises(ValueError):
        merge_intervals([[True, 2]])
    with pytest.raises(ValueError):
        merge_intervals([[0, False]])

def test_non_integer_numbers_are_rejected():
    with pytest.raises(ValueError):
        merge_intervals([[1.0, 2]])
    with pytest.raises(ValueError):
        merge_intervals([["1", 2]])

def test_coordinates_outside_range_raise_value_error():
    with pytest.raises(ValueError):
        merge_intervals([[-101, 0]])
    with pytest.raises(ValueError):
        merge_intervals([[0, 101]])

def test_twelve_intervals_allowed_thirteen_rejected():
    twelve = [[i * 2, i * 2] for i in range(12)]
    assert merge_intervals(twelve) == twelve
    thirteen = [[i * 2, i * 2] for i in range(13)]
    with pytest.raises(ValueError):
        merge_intervals(thirteen)

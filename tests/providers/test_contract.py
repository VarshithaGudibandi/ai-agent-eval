from app.providers.contract import Usage


def test_usage_is_known_only_with_input_and_output() -> None:
    assert not Usage().known
    assert not Usage(input_tokens=5).known
    assert Usage(input_tokens=5, output_tokens=0).known
    assert (
        Usage(input_tokens=5, cached_input_tokens=2, output_tokens=3, other_tokens=1).total() == 11
    )

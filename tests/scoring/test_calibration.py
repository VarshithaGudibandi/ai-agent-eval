from app.scoring.calibration import MIN_LABELS, calibrate, question_hash


def test_a_question_needs_enough_labels_and_agreement_to_be_trusted() -> None:
    agree = [("yes", "yes")] * 9 + [("no", "no")]
    assert calibrate(agree).trusted
    assert calibrate(agree).agreement == 1.0
    # Nine labels, however good, are too few.
    assert not calibrate(agree[:9]).trusted
    # Six in ten is below the bar.
    shaky = [("yes", "yes")] * 6 + [("yes", "no")] * 4
    result = calibrate(shaky)
    assert (result.labels, result.agreement, result.trusted) == (10, 0.6, False)


def test_cant_tell_answers_neither_agree_nor_count_as_labels() -> None:
    pairs = [("yes", "yes")] * MIN_LABELS + [("cant_tell", "no")] * 5
    result = calibrate(pairs)
    assert (result.labels, result.agreement, result.trusted) == (10, 1.0, True)
    assert result.cant_tell_rate == 5 / 15
    assert calibrate([]).agreement is None


def test_rewording_a_question_starts_its_calibration_again() -> None:
    assert question_hash("Is it  polite?") == question_hash("is it polite?")
    assert question_hash("Is it polite?") != question_hash("Is it kind?")

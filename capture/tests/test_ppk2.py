import numpy as np
import pytest

from capture.ppk2 import (
    ADC_MULT,
    Calibration,
    Decoder,
    PowerMode,
    dut_power_command,
    encode_samples,
    power_mode_command,
    voltage_command,
)

# Real metadata from the user's PPK2 (shortened float digits).
REAL_META = """Calibrated: 0
R0: 996.7593994140625
R1: 100.69110107421875
R2: 10.166187286376953
R3: 0.950890064239502
R4: 0.05614248663187027
GS0: 0.0000000000000000001
GS1: 88.956787109375
GS2: 14.956695556640625
GS3: 2.0270068645477295
GS4: 0.06393056362867355
GI0: 1.0
GI1: 0.9757387638092041
GI2: 0.9681105613708496
GI3: 0.9608519673347473
GI4: 0.9644978046417236
O0: 96.04659271240234
O1: 64.9397964477539
O2: 56.343605041503906
O3: 42.83573913574219
O4: 74.32290649414062
VDD: 4200
HW: 35505
mode: 2
S0: -0.00000002009019794968
S1: -0.00000017078414771277
S2: -0.00000136577818921069
S3: -0.00001456755217077443
S4: 0.002238137647509575
I0: 0.00000006660540208259
I1: 0.00000023017418016025
I2: 0.00004290431024855934
I3: -0.00016679850523360074
I4: -0.008605105802416801
UG0: 1.0
UG1: 1.0
UG2: 1.0
UG3: 1.0
UG4: 1.0
IA: 58
END"""


def reference_current(word: int, cal: Calibration, vdd_mv: float) -> float:
    """Scalar port of the IRNAS/Nordic formula, to check the vectorized version."""
    rng = min((word >> 14) & 7, 4)
    adc = (word & 0x3FFF) * 4
    base = (adc - cal.o[rng]) * (ADC_MULT / cal.r[rng])
    return cal.ug[rng] * (base * (cal.gs[rng] * base + cal.gi[rng]) + (cal.s[rng] * (vdd_mv / 1000) + cal.i[rng]))


def words(adc, rng, counter, logic=0):
    return (adc & 0x3FFF) | (rng << 14) | ((counter & 0x3F) << 18) | (logic << 24)


def test_parse_real_metadata():
    cal = Calibration.parse(REAL_META)
    assert cal.r[0] == pytest.approx(996.7594, rel=1e-6)
    assert cal.gs[1] == pytest.approx(88.9568, rel=1e-5)
    assert cal.o[4] == pytest.approx(74.3229, rel=1e-5)
    assert cal.i[4] == pytest.approx(-0.0086051, rel=1e-4)
    assert cal.vdd_mv == 4200
    assert cal.mode is PowerMode.SOURCE
    assert cal.fields["IA"] == "58"  # not confused with I0..I4


def test_conversion_matches_reference():
    cal = Calibration.parse(REAL_META)
    rng_ = np.random.default_rng(0)
    w = [words(int(rng_.integers(0, 0x3FFF)), int(rng_.integers(0, 5)), i) for i in range(500)]
    dec = Decoder(cal, vdd_mv=3700, spike_filter=False)
    blk = dec.feed(np.array(w, "<u4").tobytes())
    expected = [reference_current(x, cal, 3700) for x in w]
    np.testing.assert_allclose(blk.current, expected, rtol=1e-5, atol=1e-12)
    assert blk.lost == 0 and blk.gaps == 0


def test_chunk_boundaries_do_not_matter():
    cur = np.abs(np.random.default_rng(1).normal(1e-3, 1e-4, 1000))
    data = encode_samples(cur, counter_start=5)
    whole = Decoder(spike_filter=False).feed(data).current
    dec = Decoder(spike_filter=False)
    parts = [dec.feed(data[i : i + 7]).current for i in range(0, len(data), 7)]  # odd chunk size
    np.testing.assert_array_equal(np.concatenate(parts), whole)
    np.testing.assert_allclose(whole, cur, rtol=2e-3)  # encode/decode round trip (ADC quantization)


def test_logic_bits():
    w = [words(100, 0, i, logic=i % 256) for i in range(64)]
    blk = Decoder().feed(np.array(w, "<u4").tobytes())
    assert blk.logic.tolist() == [i % 256 for i in range(64)]


def test_lost_samples_are_filled_with_nan():
    counters = list(range(10)) + list(range(13, 20))  # 3 samples missing
    w = [words(1000, 2, c) for c in counters]
    blk = Decoder(spike_filter=False).feed(np.array(w, "<u4").tobytes())
    assert blk.lost == 3 and blk.gaps == 1
    assert len(blk.current) == len(counters) + 3
    assert np.isnan(blk.current[10:13]).all()
    assert not np.isnan(blk.current[:10]).any() and not np.isnan(blk.current[13:]).any()


def test_gap_across_chunks_and_counter_wrap():
    dec = Decoder(spike_filter=False)
    dec.feed(np.array([words(1, 0, c) for c in range(60, 64)], "<u4").tobytes())
    blk = dec.feed(np.array([words(1, 0, c) for c in (1, 2)], "<u4").tobytes())  # 0 missing after wrap
    assert blk.lost == 1 and np.isnan(blk.current[0])


def test_single_corrupt_sample_is_blanked_not_a_gap():
    counters = [0, 1, 2, 40, 4, 5]
    w = [words(1000, 2, c) for c in counters]
    blk = Decoder(spike_filter=False).feed(np.array(w, "<u4").tobytes())
    assert blk.lost == 0 and blk.corrupt == 1
    assert len(blk.current) == 6 and np.isnan(blk.current[3])


def test_realigns_after_lost_byte():
    data = encode_samples(np.full(400, 1e-3), counter_start=0)
    dec = Decoder(spike_filter=False)
    blk = dec.feed(data[1:])  # first byte missing: every word is shifted
    assert blk.dropped_bytes == 3
    assert blk.gaps <= 1
    assert np.nanmean(blk.current) == pytest.approx(1e-3, rel=5e-3)


def test_spike_filter_only_touches_samples_after_range_switch():
    cur = np.r_[np.full(50, 5e-6), np.full(50, 5e-3)]  # range switch at sample 50
    data = encode_samples(cur, 0)
    raw = Decoder(spike_filter=False).feed(data).current
    filt = Decoder(spike_filter=True).feed(data).current
    changed = np.flatnonzero(~np.isclose(raw, filt))
    assert set(changed) <= {50, 51, 52}
    assert len(changed) > 0


def test_spike_filter_carry_across_chunks():
    cur = np.r_[np.full(50, 5e-6), np.full(50, 5e-3)]
    data = encode_samples(cur, 0)
    whole = Decoder().feed(data).current
    dec = Decoder()
    parts = np.concatenate([dec.feed(data[: 51 * 4]).current, dec.feed(data[51 * 4 :]).current])
    np.testing.assert_allclose(parts, whole, rtol=1e-6)


def test_commands():
    assert voltage_command(800) == bytes([0x0D, 3, 32])
    assert voltage_command(5000) == bytes([0x0D, 19, 136])
    assert power_mode_command(PowerMode.SOURCE) == bytes([0x11, 2])
    assert dut_power_command(True) == bytes([0x0C, 1])
    with pytest.raises(ValueError):
        voltage_command(5001)

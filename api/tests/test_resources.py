"""CPU and memory per service (cgroup slices) and of the machine (/proc), on a fake tree."""

from pathlib import Path

from api.resources import Resources, find_slices, host_cpu, host_extras, host_memory, spark, stats


def slice_(root: Path, name: str, mem: int, usec: int, pids: int = 3, peak: int | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "memory.current").write_text(f"{mem}\n")
    (d / "memory.peak").write_text(f"{peak or mem}\n")
    (d / "cpu.stat").write_text(f"usage_usec {usec}\nuser_usec {usec // 2}\nsystem_usec {usec // 2}\n")
    (d / "pids.current").write_text(f"{pids}\n")
    return d


def proc(tmp: Path, total_kb: int, avail_kb: int, busy: int, idle: int) -> Path:
    p = tmp / "proc"
    p.mkdir(exist_ok=True)
    (p / "meminfo").write_text(f"MemTotal:       {total_kb} kB\nMemFree:  1 kB\nMemAvailable:   {avail_kb} kB\n")
    (p / "stat").write_text(f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\ncpu0 1 2 3 4\n")
    return p


def test_finds_slices_where_either_cgroup_driver_puts_them(tmp_path):
    slice_(tmp_path / "pst.slice", "pst-api.slice", 1, 1)  # systemd
    slice_(tmp_path, "pst-db.slice", 1, 1)  # cgroupfs
    slice_(tmp_path / "pstsim.slice", "pstsim-api.slice", 1, 1)  # the simulator stack: not ours
    (tmp_path / "pst.slice" / "pst-x.scope").mkdir()
    assert sorted(find_slices(tmp_path, "pst")) == ["api", "db"]


def test_host_files(tmp_path):
    p = proc(tmp_path, 1000, 400, 300, 700)
    assert host_memory(p / "meminfo") == (1000 * 1024, 600 * 1024)
    (p / "uptime").write_text("3600.5 100.0\n")
    (p / "loadavg").write_text("1.20 0.95 0.80 2/900 12345\n")
    (p / "meminfo").write_text((p / "meminfo").read_text() + "SwapTotal:  2000 kB\nSwapFree:   1500 kB\n")
    assert host_extras(p) == {"uptime_s": 3600.5, "load": [1.2, 0.95, 0.8], "swap_total": 2000 * 1024, "swap_used": 500 * 1024}
    assert host_extras(tmp_path / "nope") == {}
    assert host_cpu(p / "stat") == (300, 1000)
    assert host_memory(tmp_path / "nope") is None


def test_samples_cpu_from_counters_and_reports_min_avg_max(tmp_path):
    root, t = tmp_path / "cg", [1000.0]
    r = Resources(root=str(root), prefix="pst", proc=str(proc(tmp_path, 8_000_000, 6_000_000, 100, 900)), clock=lambda: t[0])
    cap = slice_(root / "pst.slice", "pst-capture.slice", 200 << 20, 0, peak=300 << 20)
    r.sample()  # first sample: memory, no CPU yet (needs two counter readings)
    for i, (mem, usec) in enumerate([(210, 1_000_000), (220, 3_000_000), (230, 3_500_000)], start=1):
        t[0] = 1000.0 + 10 * i
        slice_(root / "pst.slice", "pst-capture.slice", mem << 20, usec, peak=300 << 20)
        proc(tmp_path, 8_000_000, 6_000_000 - i * 100_000, 100 + 50 * i, 900 + 50 * i)
        r.sample()
    rep = r.report(3600)
    svc = rep["services"][0]
    assert svc["name"] == "capture" and svc["running"] and svc["peak"] == 300 << 20
    assert svc["mem"] == {"now": 230 << 20, "min": 200 << 20, "avg": 215 << 20, "max": 230 << 20}
    assert [round(x, 1) for x in svc["cpu_line"]] == [10.0, 20.0, 5.0]  # 1 s of CPU in 10 s = 10 % of one core
    assert rep["host"]["mem_total"] == 8_000_000 * 1024 and rep["host"]["cpu"]["now"] == 50.0
    assert rep["host"]["warn"] == [] and rep["available"]
    # the most since the api started: CPU from the samples (the kernel has no CPU peak), the machine's memory too
    assert round(svc["cpu_peak"], 1) == 20.0 and rep["host"]["cpu_peak"] == 50.0
    assert rep["host"]["mem_peak"] == (8_000_000 - 5_700_000) * 1024 and rep["since_s"] == 30
    # a stopped container: the slice stays, empty
    (cap / "pids.current").write_text("0\n")
    t[0] += 10
    r.sample()
    assert not r.report(3600)["services"][0]["running"]


def test_warns_when_the_machine_is_nearly_full(tmp_path):
    root, t = tmp_path / "cg", [0.0]
    slice_(root / "pst.slice", "pst-api.slice", 1, 1)
    r = Resources(root=str(root), prefix="pst", proc=str(proc(tmp_path, 1000, 100, 0, 0)), clock=lambda: t[0])
    for i in range(8):
        t[0] = 10.0 * i
        proc(tmp_path, 1000, 100, 90 * i, 10 * i)  # 90 % busy, 90 % of memory used
        r.sample()
    assert r.report(3600)["host"]["warn"] == ["memory", "cpu"]


def test_no_slices_means_not_available(tmp_path):
    r = Resources(root=str(tmp_path), prefix="pst", proc=str(proc(tmp_path, 1000, 500, 1, 1)))
    r.sample()
    rep = r.report(3600)
    assert not rep["available"] and rep["services"] == [] and rep["host"]["mem_total"] == 1000 * 1024


def test_helpers():
    assert stats([None, 1, 3]) == {"now": 3, "min": 1, "avg": 2, "max": 3} and stats([None]) is None
    assert spark(list(range(10)), 5) == [0.5, 2.5, 4.5, 6.5, 8.5] and spark([1, None, 2], 5) == [1, 2]

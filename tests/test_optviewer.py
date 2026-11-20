import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from optviewer import parse_irc, parse_memory, scan_energy_values, LiveTail

ROOT = Path(__file__).resolve().parents[1]


def test_memory_usage_units_and_limits():
    text = '''MaxCore=800 MB
Max core memory ... 800 MB
MB left = 764.44
MB needed = 6.18
Memory required: 2 GB
MB in use = 35.56
Maximum memory used throughout the entire SCF-calculation: 108.7 MB
Peak memory: 1.5 GB
Memory usage: 2 MiB
Memory used: 1.024D+3 KB
RSS: 1048576 bytes
'''
    points = parse_memory(text)
    assert [p.mb for p in points] == pytest.approx([35.56, 108.7, 1500, 2.097152, 1.024, 1.048576])
    assert points[1].label == 'SCF'
    assert points[1].line == 7


def test_memory_lone_carriage_return_keeps_source_line():
    assert parse_memory('progress\rnew progress\r\nMemory used: 2 MB\r\n')[0].line == 2


IRC = '''FINAL SINGLE POINT ENERGY -100.000000123
* FORWARD IRC *
Iteration    E(Eh)      dE(kcal/mol)  max(|G|)   RMS(G)
Convergence thresholds 0.000500 0.000100
 0 -100.100000 -62.75 0.002000 0.001000
 1 -100.200000 -125.50 0.001000 0.000500
* MAXIMUM NUMBER OF ITERATIONS REACHED - STOPPING IRC RUN *
* BACKWARD IRC *
Iteration    E(Eh)      dE(kcal/mol)  max(|G|)   RMS(G)
Convergence thresholds 0.000500 0.000100
 0 -100.050000 -31.38 0.003000 0.002000
'''


def test_ongoing_irc_reconstructs_branches_once():
    points, thresholds, status = parse_irc(IRC)
    assert [p.direction for p in points] == ['Backward', 'TS', 'Forward', 'Forward']
    assert [p.step for p in points] == [1, 2, 3, 4]
    assert points[1].energy == -100.000000123
    assert thresholds == (0.0005, 0.0001)
    assert status['Forward'] == 'iteration limit reached'
    assert status['Backward'] == 'not reported'


def test_irc_summary_is_authoritative_without_duplicates():
    text = IRC + '''IRC PATH SUMMARY
Step E(Eh) dE(kcal/mol) max(|G|) RMS(G)
1 -100.050000 -31.38 0.003000 0.002000
2 -100.000000 0.000000 0.000100 0.000050 <= TS
3 -100.100000 -62.75 0.002000 0.001000
4 -100.200000 -125.50 0.001000 0.000500
'''
    points, _, _ = parse_irc(text)
    assert len(points) == 4
    assert [p.direction for p in points] == ['Backward', 'TS', 'Forward', 'Forward']
    assert [p.iteration for p in points] == [0, None, 0, 1]


def test_irc_not_reached_is_not_reported_as_converged():
    _, _, status = parse_irc(IRC + 'IRC CONVERGENCE NOT REACHED\n')
    assert status['Backward'] == 'not reported'


def test_scan_energy_values_gaussian_orca_total():
    lines = ['SCF Done: E(RB3LYP) = -100.000000 A.U. after 10 cycles',
             'FINAL SINGLE POINT ENERGY -100.010000',
             'Total Energy        :   -99.990000 Eh',
             'MaxCore=800 MB',
             'SCF Done: E(RB3LYP) = -100.020000 A.U. after 7 cycles']
    assert list(scan_energy_values(lines)) == [-100.0, -100.01, -99.99, -100.02]


def test_live_tail_incremental(tmp_path):
    path = tmp_path / 'grow.out'
    path.write_text('line1\n')
    tail = LiveTail(path)
    assert tail.poll() == ['line1']
    assert tail.poll() is None
    path.write_text('line1\nline2\n')
    assert tail.poll() == ['line2']


def test_live_tail_keeps_partial_last_line(tmp_path):
    path = tmp_path / 'grow.out'
    path.write_text('line1\npart')
    tail = LiveTail(path)
    assert tail.poll() == ['line1']
    assert tail.poll() is None
    path.write_text('line1\npartial\n')
    assert tail.poll() == ['partial']


def _live_env(close_ms=2500):
    env = dict(os.environ)
    env['OPTVIEWER_AUTOCLOSE_MS'] = str(close_ms)
    return env


def test_input_only_opens_live_window_and_saves_snapshot(tmp_path):
    source = tmp_path / 'job.out'
    source.write_text('FINAL SINGLE POINT ENERGY -100.000000\n')
    completed = subprocess.run([str(ROOT / 'optviewer.exe'), str(source)],
                               capture_output=True, env=_live_env(), timeout=60)
    assert completed.returncode == 0, completed.stderr.decode(errors='replace')
    assert b'live tracking' in completed.stderr
    assert (tmp_path / 'job_live.png').exists()
    assert (tmp_path / 'job_live.csv').read_text(encoding='utf-8-sig').strip().splitlines() == ['point,energy_eh', '1,-100.0']


def test_live_window_tracks_records_appended_mid_run(tmp_path):
    source = tmp_path / 'job.out'
    source.write_text('FINAL SINGLE POINT ENERGY -100.000000\n')
    proc = subprocess.Popen([str(ROOT / 'optviewer.exe'), str(source)],
                            env=_live_env(6000), stderr=subprocess.DEVNULL)
    time.sleep(2.0)
    with source.open('a', encoding='utf-8') as handle:
        handle.write('FINAL SINGLE POINT ENERGY -100.005000\nFINAL SINGLE POINT ENERGY -100.010000\n')
    proc.wait(timeout=60)
    rows = (tmp_path / 'job_live.csv').read_text(encoding='utf-8-sig').strip().splitlines()
    assert rows == ['point,energy_eh', '1,-100.0', '2,-100.005', '3,-100.01']


@pytest.mark.parametrize('arguments', [['--dpi', '0'], ['--csv', 'SAME_INPUT']])
def test_cli_rejects_invalid_dpi_and_source_overwrite(tmp_path, arguments):
    source = tmp_path / 'job.out'
    source.write_text('Memory used: 1 MB')
    args = [str(source), '-M', str(tmp_path / 'memory.png')]
    args += [str(source) if a == 'SAME_INPUT' else a for a in arguments]
    completed = subprocess.run([str(ROOT / 'optviewer.exe'), *args], capture_output=True, timeout=60)
    assert completed.returncode == 2
    assert source.read_text() == 'Memory used: 1 MB'


def test_cli_requires_input():
    completed = subprocess.run([str(ROOT / 'optviewer.exe')], capture_output=True, timeout=60)
    assert completed.returncode == 2


def test_memory_without_usage_does_not_create_a_plot(tmp_path):
    source = tmp_path / 'job.out'
    source.write_text('MaxCore=800 MB\nMB needed = 6.18\n')
    png = tmp_path / 'memory.png'
    result = subprocess.run([str(ROOT / 'optviewer.exe'), str(source), '-M', str(png)], capture_output=True, timeout=60)
    assert result.returncode == 1
    assert b'No reported memory usage' in result.stderr
    assert not png.exists()


@pytest.mark.parametrize('program', ['ORCA', 'Gaussian'])
def test_original_geometry_and_transition_state_plotting(tmp_path, program):
    fixture = ROOT / 'tests/fixtures' / (program.lower() + '.out')
    png = tmp_path / f'{program}.png'
    completed = subprocess.run([str(ROOT / 'optviewer.exe'), str(fixture), '-O', str(png), '--summary'], capture_output=True, timeout=120)
    assert completed.returncode == 0, completed.stderr.decode(errors='replace')
    assert png.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    assert 'energy_eh' in png.with_suffix('.csv').read_text(encoding='utf-8-sig')
    assert b'2 optimization steps' in completed.stderr

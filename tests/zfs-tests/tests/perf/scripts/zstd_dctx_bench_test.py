#!/usr/bin/env python3
# SPDX-License-Identifier: CDDL-1.0

"""Local measurement-gate regressions; no VM, pool or kernel operations."""

import argparse
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

RUNNER = Path(__file__).with_name('zstd_dctx_bench.py').resolve()
MODULE = importlib.util.spec_from_file_location('dctx_bench', RUNNER)
bench = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(bench)


def write(path, data):
    path.write_text(json.dumps(data))


def fio_data(cell, spec, counts=None):
    counts = counts or ([spec['file_bytes'] // cell['block'] * 2]
                        * cell['workers'])
    return dict(jobs=[dict(error=0, usr_cpu=1, sys_cpu=2, ctx=3, read=dict(
        total_ios=count, io_bytes=count * cell['block'],
        short_ios=0, drop_ios=0,
        runtime=spec['seconds'] * 1000,
        bw_bytes=count * cell['block'] // spec['seconds'],
        lat_ns=dict(mean=2000), clat_ns=dict(bins={'1000': count})))
        for count in counts])


def delta(create=0, reuse=0):
    keys = ('arcstats.demand_data_misses', 'arcstats.demand_metadata_misses',
            'zstd.decompress_failed', 'zstd.decompress_alloc_fail',
            'zstd.alloc_fail', 'zstd.alloc_fallback')
    result = dict.fromkeys(keys, 0)
    result.update({'zstd.decompress_context_create': create,
                   'zstd.decompress_context_reuse': reuse})
    return result


def stopped(pid):
    path = Path('/proc') / str(pid) / 'stat'
    try:
        return path.read_text().rsplit(')', 1)[1].split()[0] == 'Z'
    except FileNotFoundError:
        return True


def process_identity(pid):
    path = Path('/proc') / str(pid)
    try:
        stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        return stat[19], (path / 'cmdline').read_bytes(), int(stat[2])
    except FileNotFoundError:
        return None


def wait_stopped(pid):
    deadline = time.monotonic() + 2
    while not stopped(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return stopped(pid)


class BenchmarkGates(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.plan = self.root / 'plan.json'
        self.results = self.root / 'results'
        self.results.mkdir()
        self.diagnostics = self.results / 'validation'
        self.diagnostics.mkdir()
        self.args = argparse.Namespace(workers=[1], physical_workers=None,
                                       rounds=3, seconds=5, ramp=1, seed=111,
                                       study='sweep', builds=['reuse'],
                                       levels=['3'], corpora=['high'],
                                       max_pair_gap=600, output=self.plan)
        with redirect_stdout(io.StringIO()):
            bench.plan(self.args)
        self.spec = json.loads(self.plan.read_text())
        # Tiny synthetic evidence exercises the gates, not performance.
        self.spec['file_bytes'] = 3 * 1048576
        write(self.plan, self.spec)
        self.context = dict(
            build_identity=dict(build='reuse', compiler='gcc',
                                configure=['--disable-debug',
                                           '--disable-debuginfo'],
                                compile_commands={'caller': 'gcc -O2'},
                                modules={m: dict(path=m, sha256=m,
                                                 note_sha256=m, srcversion=m)
                                         for m in ('zfs', 'spl')}),
            fixture_sha256='fixture',
            fixture_properties={'compressratio': '2'},
            parameters={'dbuf_cache_max_bytes': '0'}, kernel='kernel',
            boot_id='boot', cpus=[1], placement={'host': 'host'},
            fio_version='fio', pool_layout=['pool'], module_epoch='epoch',
            pool_features={'feature@embedded_data': 'disabled'},
            plan_sha256=bench.digest(self.plan), runner_sha256='runner')
        for index, cell in enumerate(self.spec['cases']):
            data = fio_data(cell, self.spec)
            reads, decodes = bench.fio_reads(data, cell, self.spec)
            counts = delta(1 if cell['cache'] else decodes,
                           decodes - 1 if cell['cache'] else 0)
            context = copy.deepcopy(self.context)
            name = self.diagnostics / cell['id']
            write(Path(str(name) + '.json'), data)
            events = Path(str(name) + '-events.csv')
            events.write_text('{},,zfs_dctx_bench:decode,1,100.00\n'
                              '{},,zfs_dctx_bench:create,1,100.00\n'.format(
                                  decodes,
                                  counts['zstd.decompress_context_create']))
            proof = dict(cell, valid=True, context=context, reads=reads,
                         expected_decodes=decodes, delta=counts,
                         counts=bench.perf_counts(events),
                         fio_sha256=bench.digest(Path(str(name) + '.json')),
                         events_sha256=bench.digest(events))
            write(Path(str(name) + '.meta.json'), proof)
            name = self.results / cell['id']
            write(Path(str(name) + '.json'), data)
            meta = dict(cell, valid=True, context=context, reads=reads,
                        expected_decodes=decodes, delta=counts,
                        kstat_before=delta(), kstat_after=counts,
                        started=index * 10, finished=index * 10 + 6,
                        monotonic_started=index * 10,
                        monotonic_finished=index * 10 + 6,
                        fio_sha256=bench.digest(Path(str(name) + '.json')),
                        validation_sha256=bench.digest(
                            self.diagnostics / (cell['id'] + '.meta.json')))
            write(Path(str(name) + '.meta.json'), meta)

    def report(self):
        bench.report(argparse.Namespace(plan=self.plan, results=self.results,
                                        output=self.root / 'analysis'))

    def change_meta(self, change):
        cell = self.spec['cases'][1]
        path = self.results / (cell['id'] + '.meta.json')
        meta = json.loads(path.read_text())
        change(meta)
        write(path, meta)
        # Rebind evidence to exercise semantic checks beyond hash rejection.
        proof_path = self.diagnostics / path.name
        proof = json.loads(proof_path.read_text())
        proof['context'] = meta['context']
        write(proof_path, proof)
        meta['validation_sha256'] = bench.digest(proof_path)
        write(path, meta)

    def test_valid_report(self):
        self.report()
        summary = json.loads((self.root / 'analysis/summary.json').read_text())
        self.assertGreater(len(summary['comparisons']), 0)

    def test_missing_diagnostic_rejected(self):
        next(self.diagnostics.glob('*.meta.json')).unlink()
        with self.assertRaises(FileNotFoundError):
            self.report()

    def test_altered_diagnostic_rejected(self):
        next(self.diagnostics.glob('*-events.csv')).write_text('0')
        with self.assertRaisesRegex(RuntimeError, 'evidence changed'):
            self.report()

    def test_stale_diagnostic_rejected(self):
        path = next(self.diagnostics.glob('*.meta.json'))
        proof = json.loads(path.read_text())
        proof['context']['module_epoch'] = 'older'
        write(path, proof)
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            self.report()

    def test_all_comparison_conditions_checked(self):
        for key in ('parameters', 'kernel', 'boot_id', 'cpus', 'placement',
                    'fio_version', 'pool_layout', 'fixture_properties',
                    'pool_features'):
            with self.subTest(key=key):
                a = dict(context=copy.deepcopy(self.context))
                b = copy.deepcopy(a)
                b['context'][key] = 'changed'
                with self.assertRaises(RuntimeError):
                    bench.matching_conditions(a, b, True)
        for key in ('compiler', 'configure', 'compile_commands', 'modules'):
            with self.subTest(key=key):
                a = dict(context=copy.deepcopy(self.context))
                b = copy.deepcopy(a)
                b['context']['build_identity'][key] = {}
                with self.assertRaises((RuntimeError, KeyError)):
                    bench.matching_conditions(a, b, True)

    def test_identity_change_rejected(self):
        self.change_meta(lambda m: m['context']['build_identity'].update(
            revision='different'))
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            self.report()

    def test_cached_reads_rejected(self):
        self.change_meta(lambda m: m.update(
            delta=delta(1), kstat_after=delta(1)))
        with self.assertRaisesRegex(RuntimeError, 'every expected decode'):
            self.report()

    def test_plan_order_rejected(self):
        self.change_meta(lambda m: m.update(started=-1, monotonic_started=-1))
        with self.assertRaisesRegex(RuntimeError, 'planned order'):
            self.report()

    def test_wall_clock_step_does_not_invalidate_monotonic_order(self):
        self.change_meta(lambda m: m.update(started=-20, finished=-30))
        self.report()

    def test_mixed_partial_passes_exact(self):
        cell = dict(corpus='mixed', block=4096, workers=2)
        spec = dict(file_bytes=3 * 4096, seconds=5)
        for counts, expected in (([6, 6], 8), ([7, 8], 11), ([8, 8], 12)):
            self.assertEqual(bench.fio_reads(fio_data(cell, spec, counts),
                                             cell, spec),
                             (sum(counts), expected))
        with self.assertRaises(RuntimeError):
            bench.fio_reads(fio_data(cell, spec, [1, 12]), cell, spec)

    def test_affinity_same_prefix_in_both_modes(self):
        with patch.object(os, 'sched_getaffinity', return_value={0, 1}):
            self.assertEqual(bench.worker_cpus('1,0', 1), [1])
            self.assertEqual(bench.worker_cpus('1,0', 2), [0, 1])
            for text, workers in (('1,1', 1), ('0', 2), ('2,1', 1)):
                with self.assertRaises(RuntimeError):
                    bench.worker_cpus(text, workers)

    def test_physical_endpoint_rejects_siblings_and_missing_topology(self):
        path = self.root / 'placement.json'
        data = dict(host='host', background='emulator', controls='fixed',
                    vcpu_map={'0': dict(host_cpu=2, core=1, numa=0,
                                        siblings=[2, 8]),
                              '1': dict(host_cpu=8, core=1, numa=0,
                                        siblings=[2, 8])})
        write(path, data)
        bench.placement_evidence(path, [0, 1], [])
        with self.assertRaisesRegex(RuntimeError, 'SMT siblings'):
            bench.placement_evidence(path, [0, 1], [0, 1])
        data['vcpu_map']['1'] = dict(
            host_cpu=3, core=2, numa=0, siblings=[3, 9])
        write(path, data)
        bench.placement_evidence(path, [0, 1], [0, 1])
        del data['vcpu_map']['1']['core']
        write(path, data)
        with self.assertRaisesRegex(RuntimeError, 'topology'):
            bench.placement_evidence(path, [0, 1], [])

    def test_controls_defaults_and_balanced_order(self):
        for study in ('controls', 'sweep'):
            args = copy.copy(self.args)
            args.study, args.rounds = study, 5
            args.levels, args.builds = None, None
            args.output = self.root / (study + '.json')
            bench.plan(args)
            spec = json.loads(args.output.read_text())
            workloads = {}
            for cell in spec['cases']:
                key = (cell['round'], cell['corpus'], cell['level'],
                       cell['block'], cell['workers'])
                workloads.setdefault(key, []).append(
                    (cell['build'], cell['cache']))
            orders = list(workloads.values())
            conditions = set(orders[0])
            for a in conditions:
                for b in conditions - {a}:
                    before = sum(order.index(a) < order.index(b)
                                 for order in orders
                                 if a in order and b in order)
                    after = sum(order.index(a) > order.index(b)
                                for order in orders
                                if a in order and b in order)
                    # Per workload, odd rounds differ by at most one.
                    self.assertLessEqual(abs(before - after),
                                         len(workloads) // 5)

    def test_histogram_merging_and_invalid_rates(self):
        cell = dict(corpus='high', block=4096, workers=2)
        data = fio_data(cell, dict(file_bytes=3 * 4096, seconds=5))
        data['jobs'][1]['read']['clat_ns']['bins'] = {'10000': 6}
        self.assertEqual(bench.fio_metrics(data)['p95_us'], 10)
        data['jobs'][0]['read']['lat_ns']['mean'] = float('nan')
        with self.assertRaises(RuntimeError):
            bench.fio_metrics(data)

    def test_cache_restored_after_exception(self):
        control = self.root / 'cache'
        control.write_text('16')
        with patch.object(bench, 'parameter', return_value=control):
            with self.assertRaisesRegex(RuntimeError, 'failure'):
                with bench.cache_control(2):
                    self.assertEqual(control.read_text(), '2')
                    raise RuntimeError('failure')
        self.assertEqual(control.read_text(), '16')

    def test_compile_and_link_commands_and_overrides(self):
        module = self.root / 'module'
        module.mkdir()
        for name in ('zfs_zstd', 'zstd_decompress'):
            (module / ('.' + name + '.o.cmd')).write_text(
                'savedcmd_x := gcc -O2 -c -o {}.o {}.c\n'.format(name, name))
        (module / '.zfs.o.cmd').write_text(
            'savedcmd_x := ld -r -o zfs.o a.o\n')
        (module / '.zfs.ko.cmd').write_text(
            'savedcmd_x := ld --build-id -r -o zfs.ko zfs.o\n')
        commands = bench.compile_commands(self.root, self.root, 'gcc')
        self.assertEqual(len(commands), 4)
        other = self.root / 'different'
        (other / 'module').mkdir(parents=True)
        for path in module.iterdir():
            (other / 'module' / path.name).write_text(
                path.read_text().replace('-O2', '-O0'))
        self.assertNotEqual(
            commands, bench.compile_commands(other, other, 'gcc'))

    def test_fabricated_bandwidth_and_duration_rejected(self):
        cell = self.spec['cases'][0]
        data = fio_data(cell, self.spec)
        data['jobs'][0]['read']['bw_bytes'] *= 2
        with self.assertRaisesRegex(RuntimeError, 'bandwidth'):
            bench.fio_reads(data, cell, self.spec)
        self.change_meta(lambda m: m.update(
            monotonic_finished=m['monotonic_started'] + 0.1))
        with self.assertRaisesRegex(RuntimeError, 'duration exceeds'):
            self.report()

    def test_distant_pairs_rejected(self):
        for index, cell in enumerate(self.spec['cases']):
            path = self.results / (cell['id'] + '.meta.json')
            meta = json.loads(path.read_text())
            meta.update(started=index * 1000, finished=index * 1000 + 6,
                        monotonic_started=index * 1000,
                        monotonic_finished=index * 1000 + 6)
            write(path, meta)
        with self.assertRaisesRegex(RuntimeError, 'far apart'):
            self.report()

    def test_enabled_events_or_function_profile_rejected(self):
        (self.root / 'events').mkdir()
        (self.root / 'current_tracer').write_text('nop')
        (self.root / 'kprobe_events').write_text('')
        (self.root / 'events/enable').write_text('1')
        (self.root / 'function_profile_enabled').write_text('0')
        with patch.object(bench, 'Path', return_value=self.root), \
                patch.object(bench.os, 'geteuid', return_value=0), \
                patch.object(bench, 'output', side_effect=['kvm', 'ext4'] * 3):
            with self.assertRaisesRegex(RuntimeError, 'event tracing'):
                bench.vm_guard()
            (self.root / 'events/enable').write_text('0')
            (self.root / 'function_profile_enabled').write_text('1')
            with self.assertRaisesRegex(RuntimeError, 'function profiling'):
                bench.vm_guard()
            (self.root / 'function_profile_enabled').write_text('0')
            bench.vm_guard()

    def test_failed_validation_restores_cache_and_removes_each_probe(self):
        cell = self.spec['cases'][1]
        control = self.root / 'cache'
        control.write_text('16')
        events = []

        def event(text):
            events.append(text)
            if text.startswith('-:') and 'create' in text:
                raise OSError('injected cleanup failure')

        with patch.object(bench, 'parameter', return_value=control), \
                patch.object(bench, 'check_files'), \
                patch.object(bench, 'warm'), \
                patch.object(bench, 'stats', return_value=delta()), \
                patch.object(bench, 'run',
                             side_effect=RuntimeError('failed')), \
                patch.object(bench, 'write_event', side_effect=event):
            with self.assertRaisesRegex(RuntimeError, 'Probe cleanup failed'):
                bench.validate_case(
                    None, self.spec, cell, {}, self.context, [],
                    self.root / 'failed-validation')
        self.assertEqual(control.read_text(), '16')
        self.assertEqual(len([e for e in events if e.startswith('-:')]), 2)

    def test_measure_requires_fresh_validation(self):
        args = argparse.Namespace(output=self.root / 'timing')
        case = (self.spec, self.spec['cases'][0], {}, self.context, [])
        with patch.object(bench, 'case_context', return_value=case), \
                patch.object(bench, 'validate_case',
                             side_effect=RuntimeError('failed')) as v, \
                patch.object(bench, 'run') as timed:
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                bench.measure(args)
            v.assert_called_once()
            timed.assert_not_called()

    def test_module_identity_does_not_rewrite_artifact(self):
        elf = self.root / 'module'
        subprocess.run(
            ['gcc', '-x', 'c', '-Wl,--build-id', '-o', str(elf), '-'],
            input='int main(void) { return 0; }', text=True, check=True)
        with elf.open('ab') as stream:
            stream.write(b'DCTX_MODULE_SIGNATURE_SENTINEL')
        before, inode = elf.read_bytes(), elf.stat().st_ino
        with patch.object(bench, 'output', return_value='srcversion'):
            bench.module_identity(elf)
        self.assertEqual(elf.read_bytes(), before)
        self.assertEqual(elf.stat().st_ino, inode)

    def test_nonembedded_512_byte_pool_gates(self):
        for state in ('enabled', 'active', 'disabled'):
            with patch.object(bench, 'output', return_value=(
                    'feature@embedded_data\t' + state)):
                if state == 'disabled':
                    bench.pool_features('pool/fs')
                else:
                    with self.assertRaisesRegex(RuntimeError,
                                                'embedded_data'):
                        bench.pool_features('pool/fs')
        for ashift in (9, 12):
            with patch.object(bench, 'output', side_effect=[
                    'config:\n pool ONLINE\n disk ONLINE\nerrors: none',
                    'ashift: ' + str(ashift)]):
                if ashift == 9:
                    self.assertEqual(
                        bench.pool_layout('pool/fs')['ashifts'], [9])
                else:
                    with self.assertRaisesRegex(RuntimeError, 'ashift=9'):
                        bench.pool_layout('pool/fs')

    def test_baseline_instrumentation_is_atomic_on_mismatch(self):
        caller = self.root / 'caller.c'
        original = ('\tkstat_named_t\tzstd_stat_dec_fail;\n'
                    '\t{ "decompress_failed",\t\tKSTAT_DATA_UINT64 },\n'
                    '\t\tZSTDSTAT_ZERO(zstd_stat_dec_fail);\n'
                    '\t/* Set header type to "magicless" */\n')
        caller.write_text(original)
        subprocess.run([sys.executable, '-I', '-B', str(RUNNER),
                        'instrument-baseline', '--caller', str(caller)],
                       check=True)
        changed = caller.read_text()
        self.assertIn('ZSTDSTAT_BUMP(zstd_stat_dec_ctx_create)', changed)
        self.assertIn('ZSTDSTAT_ZERO(zstd_stat_dec_ctx_reuse)', changed)
        with self.assertRaises(RuntimeError):
            bench.instrument_baseline(argparse.Namespace(caller=caller))
        self.assertEqual(caller.read_text(), changed)
        caller.write_text(original.replace('magicless', 'changed'))
        before = caller.read_text()
        with self.assertRaises(RuntimeError):
            bench.instrument_baseline(argparse.Namespace(caller=caller))
        self.assertEqual(caller.read_text(), before)

    def test_perf_helpers_parent_only_cancellation(self):
        helper = self.root / 'real-setpgid'
        cfile = RUNNER.parents[3] / 'cmd/zfs_test_setpgid.c'
        subprocess.run(['gcc', '-o', str(helper), str(cfile)], check=True)
        launcher = self.root / 'zfs_test_setpgid'
        launcher.write_text('#!/bin/sh\necho $$ >> "$PIDFILE"\n'
                            'exec "' + str(helper) + '" "$@"\n')
        launcher.chmod(0o755)
        library = (RUNNER.parents[1] / 'perf.shlib').read_text()
        wanted = {'stop_perf_workload', 'do_perf_cleanup', 'perf_signal',
                  'run_perf_workload', 'do_collect_scripts',
                  'do_collect_scripts_stop', 'do_collect_scripts_cleanup',
                  'do_collect_scripts_wait', 'is_optional_collector',
                  'collect_group_alive'}
        functions = '\n'.join(f for f in re.findall(
            r'(?ms)^function [^\n]+\n\{.*?^\}', library)
            if f.splitlines()[0].split()[1] in wanted)
        for phase in ('startup', 'workload', 'shutdown',
                      'collector-register', 'workload-register'):
            for signum in (signal.SIGTERM, signal.SIGINT):
                with self.subTest(phase=phase, signal=signum):
                    out = self.root / ('collect-' + phase + str(signum))
                    out.mkdir()
                    pidfile = out / 'pids'
                    active_functions = functions
                    if phase.endswith('-register'):
                        anchor = ('\tcollect_pids+=("$!")' if
                                  phase == 'collector-register' else
                                  '\tperf_workload_pid=$!')
                        # Stop exactly between spawning and recording the PID.
                        # Preserve $! until the original assignment resumes.
                        barrier = ('\tprint stopped > "$OUT/registration"\n'
                                   '\tkill -STOP $$\n')
                        self.assertIn(anchor, active_functions)
                        active_functions = active_functions.replace(
                            anchor, barrier + anchor)
                    script = """
EXIT_SIGNAL=256
PERF_RUNTIME=30; SUDO_COMMAND=mock
perf_interrupted=0
typeset -a collect_pids collect_outputs collect_optional
typeset -a collect_start_files
typeset -a collect_stop_files collect_ready_files collect_killed
collect_scripts=('printf alive; exec sleep 30' collector)
function get_perf_output_dir {{ print "$OUT"; }}
function log_note {{ :; }}
function log_fail {{
    do_perf_cleanup; print cleaned > "$OUT/cleaned"; exit 1
}}
{functions}
if [[ $PHASE == startup ]]; then
    export ZFS_TEST_SETPGID_DELAY=2
fi
do_collect_scripts case || exit 2
if [[ $PHASE == workload || $PHASE == workload-register ]]; then
    run_perf_workload sleep 30
else
    print shutdown > "$OUT/phase"
    do_collect_scripts_stop
fi
""".format(functions=active_functions)
                    env = dict(os.environ, OUT=str(out), PIDFILE=str(pidfile),
                               PHASE=phase, PATH=str(self.root) + ':' +
                               os.environ['PATH'])
                    process = subprocess.Popen(['ksh', '-c', script], env=env,
                                               start_new_session=True)
                    owned = {}
                    try:
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            if pidfile.exists():
                                for text in pidfile.read_text().splitlines():
                                    if text.isdigit():
                                        pid = int(text)
                                        owned.setdefault(
                                            pid, process_identity(pid))
                            if phase.endswith('-register'):
                                wanted_pids = (1 if phase ==
                                               'collector-register' else 2)
                                ready = (len(owned) == wanted_pids and
                                         (out / 'registration').exists() and
                                         '\nState:\tT' in
                                         Path('/proc', str(process.pid),
                                              'status').read_text())
                            else:
                                ready = (
                                    bool(owned) if phase == 'startup' else
                                    len(owned) == 2 and
                                    any(out.glob('fio-ready.*')) and all(
                                        process_identity(pid)[2] == pid
                                        for pid in owned)
                                    if phase == 'workload' else
                                    (out / 'phase').exists())
                            if ready:
                                break
                            time.sleep(0.02)
                        self.assertTrue(ready)
                        process.send_signal(signum)
                        if phase.endswith('-register'):
                            process.send_signal(signal.SIGCONT)
                        process.wait(timeout=10)
                        self.assertNotEqual(process.returncode, 0)
                        self.assertTrue((out / 'cleaned').exists())
                        for pid in owned:
                            self.assertTrue(wait_stopped(pid))
                        self.assertFalse(any(out.glob('*.ready')))
                        self.assertFalse(any(out.glob('fio-ready.*')))
                    finally:
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=3)
                        for pid, initial in owned.items():
                            current = process_identity(pid)
                            if (initial and current and
                                    initial[0] == current[0]):
                                if current[2] == pid:
                                    os.killpg(pid, signal.SIGKILL)
                                else:
                                    os.kill(pid, signal.SIGKILL)

    def test_shell_root_guard_stops_before_pool_operations(self):
        virt = self.root / 'systemd-detect-virt'
        virt.write_text('#!/bin/sh\necho kvm\n')
        virt.chmod(0o755)
        wrapper = RUNNER.parents[1] / 'regression/zstd_decompress.ksh'
        source = wrapper.read_text()
        guards = source[source.index('verify_runnable "global"'):source.index(
            'function cleanup')]
        for result in ('return 1', 'print zfs', 'print ext4'):
            script = """
function verify_runnable {{ return 0; }}
function is_linux {{ return 0; }}
function findmnt {{ {result}; }}
function log_unsupported {{ exit 77; }}
{guards}
print pool-operations
""".format(result=result, guards=guards)
            env = dict(os.environ, PATH=str(self.root) + ':' +
                       os.environ['PATH'])
            process = subprocess.run(['ksh', '-c', script], text=True, env=env,
                                     stdout=subprocess.PIPE, check=False)
            self.assertEqual(process.returncode, 0 if result == 'print ext4'
                             else 77)
            self.assertEqual(process.stdout.strip(), 'pool-operations'
                             if result == 'print ext4' else '')

    def test_parent_only_shell_signals_wait_before_destroy(self):
        wrapper = RUNNER.parents[1] / 'regression/zstd_decompress.ksh'
        source = wrapper.read_text()
        functions = source[source.index('function cleanup'):source.index(
            'log_onexit cleanup')]
        for phase, signum in [(p, s) for p in ('registered', 'registration')
                              for s in (signal.SIGTERM, signal.SIGINT)]:
            with self.subTest(phase=phase, signal=signum):
                tag = phase + '-' + str(signum)
                control = self.root / ('shell-cache-' + tag)
                control.write_text('16')
                log = self.root / ('shell-log-' + tag)
                ready = self.root / ('shell-ready-' + tag)
                driver = self.root / ('shell-driver-' + tag + '.py')
                driver.write_text("""
import importlib.util, os, pathlib, signal, sys
spec = importlib.util.spec_from_file_location('bench', {!r})
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
signal.signal(signal.SIGTERM, b.interrupt)
signal.signal(signal.SIGINT, b.interrupt)
b.parameter = lambda name: pathlib.Path(os.environ['CONTROL'])
try:
    with b.cache_control(2):
        b.run([sys.executable, '-I', '-c',
               'import os,pathlib,time; pathlib.Path(' +
               repr(os.environ['READY']) +
               ').write_text(str(os.getpid())); time.sleep(60)'])
except RuntimeError:
    pass
finally:
    with pathlib.Path(os.environ['LOG']).open('a') as out:
        out.write('runner-restored\\n')
""".format(str(RUNNER)))
                # Empty saved tunables ensure this mock never touches sysfs.
                active_functions = functions
                if phase == 'registration':
                    anchor = '\trunner_pid=$!'
                    barrier = ('\tprint stopped > "$LOG.barrier"\n'
                               '\tkill -STOP $$\n')
                    self.assertIn(anchor, functions)
                    active_functions = functions.replace(
                        anchor, barrier + anchor)
                shell = """
dbuf_before=; prefetch_before=; PERFPOOL=mock
runner=$1
function poolexists {{ return 0; }}
function destroy_pool {{ print pool-destroyed >> "$LOG"; }}
{functions}
function log_fail {{ cleanup; exit 1; }}
trap 'log_fail interrupted' SIGTERM SIGINT
run_runner
""".format(functions=active_functions)
                env = dict(os.environ, CONTROL=str(control), LOG=str(log),
                           READY=str(ready))
                process = subprocess.Popen(['ksh', '-c', shell, 'test',
                                            str(driver)], env=env,
                                           start_new_session=True)
                child_pid, child_identity = None, None
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        runner_ready = (ready.exists() and
                                        ready.read_text().strip())
                        barrier_ready = (phase == 'registered' or
                                         (Path(str(log) + '.barrier').exists()
                                          and '\nState:\tT' in
                                          Path('/proc', str(process.pid),
                                               'status').read_text()))
                        if runner_ready and barrier_ready:
                            break
                        time.sleep(0.02)
                    self.assertTrue(ready.exists())
                    child_pid = int(ready.read_text())
                    child_identity = process_identity(child_pid)
                    process.send_signal(signum)
                    if phase == 'registration':
                        process.send_signal(signal.SIGCONT)
                    process.wait(timeout=8)
                    self.assertNotEqual(process.returncode, 0)
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=3)
                    if child_pid is not None and not stopped(child_pid):
                        current = process_identity(child_pid)
                        if current is not None and current == child_identity:
                            os.killpg(current[2], signal.SIGKILL)
                self.assertEqual(control.read_text(), '16')
                self.assertEqual(log.read_text().splitlines(),
                                 ['runner-restored', 'pool-destroyed'])

    def test_cancellation_and_failed_wrapper_reap_descendants(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                control = self.root / ('cache-' + str(fail))
                control.write_text('16')
                pidfile = self.root / ('pid-' + str(fail))
                ready = self.root / ('ready-' + str(fail))
                child = (
                    'import os,signal,time; from pathlib import Path; '
                    'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                    'pid=os.fork(); '
                    'Path({!r}).write_text(str(pid)) if pid else None; '
                    'os._exit(1) if pid and {} else time.sleep(60)'
                ).format(str(pidfile), fail)
                driver = '''
import importlib.util, pathlib, signal, sys
spec = importlib.util.spec_from_file_location('bench', sys.argv[1])
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
signal.signal(signal.SIGTERM, b.interrupt)
b.parameter = lambda name: pathlib.Path(sys.argv[2])
try:
    with b.cache_control(2):
        pathlib.Path(sys.argv[3]).touch()
        b.run([sys.executable, '-I', '-c', sys.argv[4]])
except RuntimeError:
    pass
'''
                process = subprocess.Popen([sys.executable, '-I', '-c', driver,
                                            str(RUNNER), str(control),
                                            str(ready), child])
                descendant, identity = None, None
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if pidfile.exists() and pidfile.read_text().strip():
                            break
                        time.sleep(0.02)
                    self.assertTrue(ready.exists() and pidfile.exists())
                    descendant = int(pidfile.read_text())
                    identity = process_identity(descendant)
                    if not fail:
                        process.send_signal(signal.SIGTERM)
                    process.wait(timeout=8)
                    self.assertEqual(process.returncode, 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=3)
                    if descendant is not None and not stopped(descendant):
                        current = process_identity(descendant)
                        if current is not None and current == identity:
                            os.killpg(current[2], signal.SIGKILL)
                self.assertEqual(control.read_text(), '16')
                self.assertTrue(wait_stopped(descendant))


if __name__ == '__main__':
    unittest.main()

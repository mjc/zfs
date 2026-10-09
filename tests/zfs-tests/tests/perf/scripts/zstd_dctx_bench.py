#!/usr/bin/env python3
# SPDX-License-Identifier: CDDL-1.0
#
# This file and its contents are supplied under the terms of the
# Common Development and Distribution License ("CDDL"), version 1.0.
# You may only use this file in accordance with the terms of version
# 1.0 of the CDDL.
#
# A full copy of the text of the CDDL should have accompanied this
# source.  A copy of the CDDL is also available via the Internet at
# https://opensource.org/license/CDDL-1.0.

"""Plan, run and compare real ZFS DCtx workloads in a disposable Linux VM.

The caller owns the test pool and module switching. This program never
creates/destroys pools or reloads modules. Profiling occurs in separate
diagnostic subruns, never the timed interval. See zstd_dctx_bench.md.
"""

import argparse
from contextlib import contextmanager
import csv
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import shlex
import shutil
import statistics
import subprocess
import tempfile
import time


CANCELLED = False


def interrupt(signum, frame):
    global CANCELLED
    CANCELLED = True


@contextmanager
def cleanup_signals():
    old = signal.pthread_sigmask(
        signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


def run(args, **kwargs):
    require(not CANCELLED, 'Benchmark interrupted')
    with subprocess.Popen([str(a) for a in args], start_new_session=True,
                          **kwargs) as child:
        failed = True
        try:
            while True:
                require(not CANCELLED, 'Benchmark interrupted')
                try:
                    stdout, stderr = child.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    pass
            require(not CANCELLED, 'Benchmark interrupted')
            require(child.returncode == 0,
                    'Command failed: ' + str(args[0]))
            failed = False
            return stdout
        finally:
            with cleanup_signals():
                if child.poll() is None or CANCELLED or failed:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    except ProcessLookupError:
                        pass
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()


def output(args):
    return run(args, stdout=subprocess.PIPE, text=True).strip()


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024**2), b''):
            result.update(chunk)
    return result.hexdigest()


def save(path, data):
    with path.open('x') as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.write('\n')


def new_case(out, ident):
    suffixes = ('.json', '.meta.json', '-warm.json', '-ramp.json',
                '-events.csv')
    require(not any((out / (ident + s)).exists() for s in suffixes),
            'Case output already exists: ' + ident)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def vm_guard():
    require(os.geteuid() == 0, 'Run inside the disposable guest as root')
    require(output(['systemd-detect-virt']) == 'kvm',
            'This runner requires a disposable KVM guest')
    require(output(['findmnt', '-n', '-o', 'FSTYPE', '/']) != 'zfs',
            'Refusing a guest with a ZFS root')
    trace = Path('/sys/kernel/tracing')
    require((trace / 'current_tracer').read_text().strip() == 'nop',
            'Disable tracing before timing')
    require(not (trace / 'kprobe_events').read_text().strip(),
            'Remove diagnostic kprobes before timing')
    require((trace / 'events/enable').read_text().strip() == '0',
            'Disable event tracing before timing')
    profile = trace / 'function_profile_enabled'
    require(not profile.exists() or profile.read_text().strip() == '0',
            'Disable function profiling before timing')


def stats():
    values = {}
    for group in ('arcstats', 'zstd'):
        path = Path('/proc/spl/kstat/zfs') / group
        for line in path.read_text().splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[1].isdigit():
                values[group + '.' + fields[0]] = int(fields[2])
    return values


def parameter(name):
    return Path('/sys/module/zfs/parameters') / name


CORPORA = ('high', 'moderate', 'incompressible', 'mixed')
BUILDS = ('baseline', 'reuse', 'hoisted-baseline', 'hoisted-reuse')


def fixture_name(corpus, level, block):
    return 'p{}-l{}-b{}'.format(corpus, level, block)


def corpus_blocks(corpus, block, file_bytes, seed):
    """Equal mixed record counts; reproducible nonzero, distinct records."""
    require(file_bytes % (3 * block) == 0, 'Use a multiple of three records')
    rng = random.Random(seed)
    for index in range(file_bytes // block):
        kind = CORPORA[index % 3] if corpus == 'mixed' else corpus
        pattern = hashlib.sha256(str(index).encode()).digest()
        repeats = (pattern * (block // len(pattern) + 1))[:block]
        if kind == 'high':
            yield repeats
        elif kind == 'moderate':
            # Compressible prefix prevents the ZFS early-abort heuristic
            # from treating the whole record as incompressible.
            yield repeats[:block // 2] + rng.getrandbits(
                block // 2 * 8).to_bytes(block // 2, 'little')
        else:
            yield rng.getrandbits(block * 8).to_bytes(block, 'little')


def fixture_properties(dataset):
    text = output(['zfs', 'get', '-H', '-p', '-o', 'property,value',
                   'compression,compressratio,recordsize,dedup,atime,'
                   'checksum,secondarycache', dataset])
    return dict(line.split('\t') for line in text.splitlines())


def check_fixture(item):
    require(fixture_properties(item['dataset']) == item['properties'],
            'Fixture properties or compression ratio changed')


def fio_args(directory, block, workers, file_bytes, cpus):
    return ['fio', '--eta=never', '--name=read', '--ioengine=psync',
            '--thread=1', '--direct=0', '--invalidate=0', '--rw=read',
            '--filename_format=file.$jobnum', '--size=' + str(file_bytes),
            '--group_reporting=0', '--output-format=json+',
            '--directory=' + str(directory), '--bs=' + str(block),
            '--numjobs=' + str(workers), '--cpus_allowed=' + cpus,
            '--cpus_allowed_policy=split', '--percentile_list=50:95:99']


def plan(args):
    args.levels = args.levels or (
        ['3'] if args.study == 'controls' else ['fast', '3', '7', '19'])
    workers = sorted(set(args.workers))
    require(workers and min(workers) > 0, 'Worker counts must be positive')
    require(args.rounds >= 3 and args.seconds >= 5 and args.ramp >= 1,
            'Use at least three rounds, five seconds and one second warmup')
    require(args.max_pair_gap > args.seconds, 'Pair gap is too short')
    builds = args.builds or (
        list(BUILDS) if args.study == 'controls' else ['hoisted-reuse'])
    for values in (builds, args.levels, args.corpora):
        require(len(values) == len(set(values)), 'Duplicate matrix values')
    physical = args.physical_workers
    require(physical is None or physical in workers,
            'Include the verified physical-core count in workers')
    blocks = (4096, 131072, 1048576)
    cells = {}
    if args.study == 'controls':
        require(builds == list(BUILDS), 'Controls require all four builds')
        require(args.levels == ['3'], 'Controls use level 3 only')
        for corpus, block, count in itertools.product(
                args.corpora, blocks, sorted({1, max(workers)})):
            cells[(corpus, '3', block, count)] = [
                (build, cache) for build in builds
                for cache in ((0, 16) if build.endswith('reuse') else (0,))]
    else:
        require(len(builds) == 1 and builds[0].endswith('reuse'),
                'Sweep requires one candidate build')
        representative = sorted({1, max(workers)} | (
            {physical} if physical is not None else set()))
        for corpus, level, block, count in itertools.product(
                args.corpora, args.levels, blocks, representative):
            cells[(corpus, level, block, count)] = [(builds[0], c)
                                                    for c in (0, 16)]
        scale_level = '3' if '3' in args.levels else args.levels[0]
        for corpus, block, count in itertools.product(
                args.corpora, (4096, 1048576), workers):
            cells[(corpus, scale_level, block, count)] = [
                (builds[0], c) for c in (0, 2, 4, 8, 16)]
    cases = []
    for repeat in range(1, args.rounds + 1):
        workloads = sorted(cells)
        random.Random(args.seed + repeat).shuffle(workloads)
        for corpus, level, block, count in workloads:
            order = cells[(corpus, level, block, count)]
            pivot = ((repeat - 1) // 2) % len(order)
            order = order[pivot:] + order[:pivot]
            if repeat % 2 == 0:
                order.reverse()
            # A whole paired workload stays adjacent, including build switches.
            for build, cache in order:
                ident = ('r{}-{}-c{}-p{}-l{}-b{}-w{}'.format(
                    repeat, build, cache, corpus, level, block, count))
                cases.append(dict(id=ident, round=repeat, build=build,
                                  cache=cache, level=level, block=block,
                                  corpus=corpus, workers=count))
    save(args.output, dict(schema=3, study=args.study, seed=args.seed,
                           rounds=args.rounds, seconds=args.seconds,
                           ramp=args.ramp, max_pair_gap=args.max_pair_gap,
                           file_bytes=18 * 1024**2, workers=workers,
                           physical_workers=physical, levels=args.levels,
                           corpora=args.corpora, blocks=blocks, cases=cases))
    print('{} cases; {:.1f} hours of timed/diagnostic work, plus warmup/'
          'switching'.format(len(cases), len(cases) *
                             (2 * args.seconds + 2 * args.ramp) / 3600))


def prepare(args):
    vm_guard()
    spec = json.loads(args.plan.read_text())
    pool_features(args.dataset)
    pool_layout(args.dataset)
    parent = output(['zfs', 'get', '-H', '-o', 'value', 'mountpoint',
                     args.dataset])
    require(Path(parent).is_dir(), 'Dataset must already be mounted')
    out = args.output
    out.mkdir()
    fixtures = {}
    for corpus, level, block in itertools.product(
            spec['corpora'], spec['levels'], spec['blocks']):
        name = fixture_name(corpus, level, block)
        dataset = args.dataset + '/' + name
        directory = Path(parent) / name
        compression = 'zstd-fast' if level == 'fast' else 'zstd-' + level
        run(['zfs', 'create', '-o', 'recordsize=' + str(block),
             '-o', 'compression=' + compression, '-o', 'atime=off',
             '-o', 'checksum=sha256', '-o', 'dedup=off',
             '-o', 'secondarycache=none',
             '-o', 'mountpoint=' + str(directory), dataset])
        count = max(spec['workers'])
        before = stats()
        hashes = {}
        for index in range(count):
            path = directory / ('file.' + str(index))
            digest = hashlib.sha256()
            with path.open('xb') as stream:
                for data in corpus_blocks(corpus, block, spec['file_bytes'],
                                          spec['seed'] + index):
                    stream.write(data)
                    digest.update(data)
                stream.flush()
                os.fsync(stream.fileno())
            hashes[str(path)] = digest.hexdigest()
        run(['zpool', 'sync', args.dataset.split('/')[0]])
        after = stats()
        delta = {k: after[k] - before[k] for k in before}
        for key in ('zstd.compress_alloc_fail', 'zstd.compress_failed',
                    'zstd.alloc_fail', 'zstd.alloc_fallback'):
            require(delta[key] == 0, 'Preparation failure: ' + key)
        fixtures[name] = dict(dataset=dataset, directory=str(directory),
                              sha256=hashes, preparation_delta=delta,
                              properties=fixture_properties(dataset))
        print('Prepared', name, flush=True)
    run(['zpool', 'sync', args.dataset.split('/')[0]])
    save(out / 'fixture.json', dict(plan_sha256=hashlib.sha256(
        args.plan.read_bytes()).hexdigest(), fixtures=fixtures,
        pool_features=pool_features(args.dataset),
        properties=output(['zfs', 'get', '-r', 'compression,compressratio,'
                           'recordsize,used,logicalused', args.dataset])))


def verify(args):
    vm_guard()
    fixture = json.loads(args.fixture.read_text())
    for item in fixture['fixtures'].values():
        check_fixture(item)
        require(pool_features(item['dataset']) == fixture['pool_features'],
                'Pool feature state changed')
        for filename, wanted in item['sha256'].items():
            with Path(filename).open('rb') as stream:
                actual = hashlib.sha256(stream.read()).hexdigest()
            require(actual == wanted, 'Checksum mismatch: ' + filename)
    print('All fixture checksums passed', flush=True)


def module_identity(path):
    before = digest(path)
    with tempfile.TemporaryDirectory() as scratch:
        note = Path(scratch) / 'note'
        run(['objcopy', '--dump-section', '.note.gnu.build-id=' + str(note),
             path, Path(scratch) / 'module-copy'])
        require(note.exists() and note.stat().st_size > 0,
                'Module must have a GNU build ID')
        require(digest(path) == before,
                'Module changed while reading identity')
        return dict(path=str(path), sha256=before, note_sha256=digest(note),
                    srcversion=output(['modinfo', '-F', 'srcversion', path]))


def compile_commands(build_root, source, compiler):
    commands = {}
    tool = shutil.which(compiler)
    require(tool is not None, 'Compiler is unavailable')
    compiler_path = Path(tool).resolve()
    roots = {str(source): '$SOURCE'}
    if build_root != source:
        roots[str(build_root)] = '$BUILD'
    for path in (build_root / 'module').rglob('.*.cmd'):
        if not path.name.endswith(('.o.cmd', '.ko.cmd')):
            continue
        lines = [line.split(' := ', 1)[1]
                 for line in path.read_text().splitlines()
                 if line.startswith(('cmd_', 'savedcmd_')) and ' := ' in line]
        require(len(lines) == 1,
                'Missing or ambiguous Kbuild command: ' + str(path))
        tokens = shlex.split(lines[0])
        # Aggregate .o commands use the linker; retain them without calling
        # them compiler invocations.
        if '-c' in tokens:
            actual = shutil.which(tokens[0])
            require(actual is not None
                    and Path(actual).resolve() == compiler_path,
                    'Manifest compiler does not match Kbuild commands')
        command = lines[0]
        for root in sorted(roots, key=len, reverse=True):
            command = command.replace(root, roots[root])
        commands[str(path.relative_to(build_root))] = command
    require(any('zfs_zstd.o' in path and '-c' in shlex.split(cmd)
                for path, cmd in commands.items())
            and any('zstd_decompress.o' in path and '-c' in shlex.split(cmd)
                    for path, cmd in commands.items()),
            'Keep actual Kbuild .cmd files with the build')
    return commands


def manifest(args):
    """Bind a release build's provenance to its loadable ELF identities."""
    configure = shlex.split(
        output([args.config_status.resolve(), '--config']))
    require('--disable-debug' in configure
            and '--disable-debuginfo' in configure
            and '--enable-debug' not in configure
            and '--enable-debuginfo' not in configure,
            'Use explicit matching release configurations')
    modules = {}
    for entry in args.module:
        name, filename = entry.split('=', 1)
        require(name not in modules, 'Duplicate module')
        path = Path(filename).resolve()
        modules[name] = module_identity(path)
    require(set(modules) == {'zfs', 'spl'}, 'Provide zfs and spl modules')
    source = args.source.resolve()
    build_root = args.config_status.resolve().parent
    commands = compile_commands(build_root, source, args.compiler)
    save(args.output, dict(build=args.build, modules=modules,
                           compiler=output([args.compiler, '--version']),
                           configure=configure,
                           compile_commands=commands,
                           revision=output(['git', '-C', source,
                                            'rev-parse', 'HEAD']),
                           patch_sha256=hashlib.sha256(run([
                               'git', '-C', source, 'diff', '--binary',
                               'HEAD'],
                               stdout=subprocess.PIPE)).hexdigest()))


def worker_cpus(text, workers):
    cpus = [int(cpu) for cpu in text.split(',')]
    require(len(cpus) >= workers and len(set(cpus)) == len(cpus),
            'Not enough distinct worker CPUs')
    require(set(cpus) <= os.sched_getaffinity(0),
            'Requested worker CPU is unavailable')
    # fio splits the supplied mask in ascending CPU order.
    return sorted(cpus[:workers])


def pool_layout(dataset):
    status = output(['zpool', 'status', '-P', dataset.split('/')[0]])
    config = status.split('config:', 1)[1].split('errors:', 1)[0]
    tree = output(['zdb', '-C', dataset.split('/')[0]])
    ashifts = list(map(int, re.findall(r'ashift: (\d+)', tree)))
    require(ashifts and all(shift == 9 for shift in ashifts),
            '4 KiB compression requires a 512-byte/ashift=9 test layout')
    return dict(vdevs=[line.split()[:2] for line in config.splitlines()
                       if line.strip()], ashifts=ashifts)


def pool_features(dataset):
    text = output(['zpool', 'get', '-H', '-o', 'property,value', 'all',
                   dataset.split('/')[0]])
    features = dict(line.split('\t') for line in text.splitlines()
                    if line.startswith('feature@'))
    require(features.get('feature@embedded_data') == 'disabled',
            'Create the benchmark pool with embedded_data disabled')
    return features


def instrument_baseline(args):
    """Add matching accounting to an explicitly supplied baseline snapshot."""
    text = args.caller.read_text()
    require('decompress_context_create' not in text,
            'Use the pre-reuse baseline source, not a candidate')
    changes = {
        '\tkstat_named_t\tzstd_stat_dec_fail;':
        '\tkstat_named_t\tzstd_stat_dec_fail;\n'
        '\tkstat_named_t\tzstd_stat_dec_ctx_create;\n'
        '\tkstat_named_t\tzstd_stat_dec_ctx_reuse;',
        '\t{ "decompress_failed",\t\tKSTAT_DATA_UINT64 },':
        '\t{ "decompress_failed",\t\tKSTAT_DATA_UINT64 },\n'
        '\t{ "decompress_context_create",\tKSTAT_DATA_UINT64 },\n'
        '\t{ "decompress_context_reuse",\tKSTAT_DATA_UINT64 },',
        '\t\tZSTDSTAT_ZERO(zstd_stat_dec_fail);':
        '\t\tZSTDSTAT_ZERO(zstd_stat_dec_fail);\n'
        '\t\tZSTDSTAT_ZERO(zstd_stat_dec_ctx_create);\n'
        '\t\tZSTDSTAT_ZERO(zstd_stat_dec_ctx_reuse);',
        '\t/* Set header type to "magicless" */':
        '\tZSTDSTAT_BUMP(zstd_stat_dec_ctx_create);\n\n'
        '\t/* Set header type to "magicless" */'}
    for old, new in changes.items():
        require(text.count(old) == 1, 'Baseline source layout changed')
        text = text.replace(old, new)
    args.caller.write_text(text)


def placement_evidence(path, cpus, physical):
    placement = json.loads(path.read_text())
    require(placement.get('host') and placement.get('vcpu_map')
            and placement.get('background') and placement.get('controls'),
            'Supply host placement and background/control evidence')
    mapping = placement['vcpu_map']
    require(set(map(int, mapping)) >= set(cpus),
            'Placement evidence omits a worker CPU')
    active = [mapping[str(cpu)] for cpu in cpus]
    keys = ('host_cpu', 'core', 'siblings', 'numa')
    for entry in active:
        require(all(k in entry for k in keys)
                and all(type(entry[k]) is int and entry[k] >= 0
                        for k in ('host_cpu', 'core', 'numa'))
                and entry['host_cpu'] in entry['siblings']
                and all(type(cpu) is int and cpu >= 0
                        for cpu in entry['siblings'])
                and len(set(entry['siblings']))
                == len(entry['siblings']),
                'Incomplete or contradictory host CPU topology')
    require(len({e['host_cpu'] for e in active}) == len(active),
            'Worker vCPUs share a host CPU')
    for a, b in itertools.combinations(active, 2):
        overlap = set(a['siblings']) & set(b['siblings'])
        require(not overlap or (set(a['siblings']) == set(b['siblings'])
                                and a['core'] == b['core']
                                and a['numa'] == b['numa']),
                'Contradictory sibling/core/NUMA mapping')
    cores = [set(mapping[str(cpu)]['siblings']) for cpu in physical]
    require(all(not a & b for a, b in itertools.combinations(cores, 2)),
            'Physical-core endpoint contains SMT siblings')
    return placement


def case_context(args):
    vm_guard()
    spec = json.loads(args.plan.read_text())
    require(spec['schema'] == 3, 'Regenerate the plan with this runner')
    cell = next(c for c in spec['cases'] if c['id'] == args.case)
    fixture = json.loads(args.fixture.read_text())
    require(fixture['plan_sha256'] == digest(args.plan),
            'Fixture and plan disagree')
    item = fixture['fixtures'][fixture_name(
        cell['corpus'], cell['level'], cell['block'])]
    check_fixture(item)
    features = pool_features(item['dataset'])
    require(features == fixture['pool_features'], 'Pool feature state changed')
    directory = item['directory']
    require(output(['findmnt', '-n', '-o', 'FSTYPE', '-T', directory]) == 'zfs'
            and output(['findmnt', '-n', '-o', 'SOURCE', '-T', directory])
            == item['dataset'], 'Wrong fixture filesystem or dataset')
    build = json.loads(args.build_manifest.read_text())
    require(build['build'] == cell['build'], 'Wrong build label')
    require(set(build['modules']) == {'zfs', 'spl'},
            'Incomplete build identity')
    require('--disable-debug' in build['configure']
            and '--disable-debuginfo' in build['configure'],
            'Release build manifest is required')
    for module, identity in build['modules'].items():
        loaded = Path('/sys/module') / module
        require(digest(Path(identity['path'])) == identity['sha256']
                and digest(loaded / 'notes/.note.gnu.build-id')
                == identity['note_sha256']
                and (loaded / 'srcversion').read_text().strip()
                == identity['srcversion'], 'Loaded module identity mismatch')
    params = {p.name: p.read_text().strip() for p in parameter('').iterdir()
              if p.name != 'zfs_zstd_cache_max'}
    require(params['dbuf_cache_max_bytes'] == '0'
            and params['zfs_prefetch_disable'] == '1'
            and params['zfs_compressed_arc_enabled'] == '1',
            'Require dbuf cache off, prefetch off and compressed ARC on')
    require({'zstd.decompress_context_create', 'zstd.decompress_context_reuse'}
            <= stats().keys(), 'Apply counter-only patch to baseline builds')
    cpus = worker_cpus(args.cpus, cell['workers'])
    # Even small-capacity cases share a guest with at least four online CPUs:
    # the initialized slot array is bounded by min(boot_ncpus * 4, 16).
    require(os.cpu_count() >= 4, 'Use at least four guest CPUs for 16 slots')
    all_cpus = worker_cpus(args.cpus, max(spec['workers']))
    physical = (worker_cpus(args.cpus, spec['physical_workers'])
                if spec['physical_workers'] is not None else [])
    placement = placement_evidence(args.placement, all_cpus, physical)
    context = dict(build_identity=build, cpus=cpus,
                   fixture_sha256=digest(args.fixture),
                   fixture_properties=item['properties'],
                   plan_sha256=digest(args.plan), parameters=params,
                   kernel=output(['uname', '-a']),
                   boot_id=Path('/proc/sys/kernel/random/boot_id')
                   .read_text().strip(), placement=placement,
                   module_epoch=Path('/proc/spl/kstat/zfs/zstd')
                   .read_text().splitlines()[0].split()[5],
                   fio_version=output(['fio', '--version']),
                   runner_sha256=digest(Path(__file__)),
                   pool_layout=pool_layout(item['dataset']))
    context['pool_features'] = features
    command = fio_args(directory, cell['block'], cell['workers'],
                       spec['file_bytes'], ','.join(map(str, cpus)))
    command += ['--allow_file_create=0', '--readonly']
    context['fio_command'] = command
    return spec, cell, item, context, command


def check_files(item, workers):
    for index in range(workers):
        path = Path(item['directory']) / ('file.' + str(index))
        require(digest(path) == item['sha256'][str(path)],
                'Fixture checksum mismatch: ' + str(path))


@contextmanager
def cache_control(cache):
    control = parameter('zfs_zstd_cache_max')
    old = control.read_text().strip() if control.exists() else None
    require(old is not None or cache == 0, 'Requested cache is unavailable')
    try:
        if old is not None:
            control.write_text(str(cache))
            require(int(control.read_text()) == cache,
                    'Cache parameter did not take effect')
        yield
    finally:
        with cleanup_signals():
            if old is not None:
                control.write_text(old)


def check_delta(delta, cache, corpus, expected):
    for key in ('arcstats.demand_data_misses',
                'arcstats.demand_metadata_misses',
                'zstd.decompress_failed', 'zstd.decompress_alloc_fail',
                'zstd.alloc_fail', 'zstd.alloc_fallback'):
        require(delta[key] == 0, '{} advanced by {}'.format(key, delta[key]))
    create = delta['zstd.decompress_context_create']
    reuse = delta['zstd.decompress_context_reuse']
    require(create >= 0 and reuse >= 0 and create + reuse == expected,
            'Context counts do not account for every expected decode')
    if expected:
        require((reuse == 0) if cache == 0 else (reuse > 0),
                'Context activity disagrees with cache setting')
    return create, reuse


def fio_reads(data, cell, spec, sustained=True):
    jobs = data['jobs']
    require(len(jobs) == cell['workers'], 'Missing individual fio jobs')
    reads, decodes = 0, 0
    records = spec['file_bytes'] // cell['block']
    for job in jobs:
        read = job['read']
        count = read['total_ios']
        require(type(count) is int and math.isfinite(read['runtime'])
                and read['runtime'] > 0
                and read['bw_bytes']
                == read['io_bytes'] * 1000 // read['runtime'],
                'Invalid fio byte count, bandwidth or duration')
        require(job['error'] == 0 and read['short_ios'] == 0
                and read['drop_ios'] == 0
                and read['io_bytes'] == count * cell['block'],
                'Incomplete fio reads')
        require((count >= 2 * records
                 and read['runtime'] >= spec['seconds'] * 1000)
                if sustained else count == records,
                'Every worker must read the full fixture repeatedly')
        reads += count
        decodes += (0 if cell['corpus'] == 'incompressible' else
                    count // 3 * 2 + min(count % 3, 2)
                    if cell['corpus'] == 'mixed' else count)
    return reads, decodes


def warm(command, name, spec):
    run(command + ['--output=' + str(name) + '-warm.json'])
    run(command + ['--time_based=1', '--runtime=' + str(spec['ramp']),
                   '--output=' + str(name) + '-ramp.json'])


def perf_counts(path):
    counts = {}
    for row in csv.reader(path.read_text().splitlines()):
        if not row or row[0].startswith('#'):
            continue
        require(len(row) >= 5 and not row[0].startswith('<')
                and float(row[4]) >= 99.9 and row[2] not in counts,
                'Validation events were not counted completely')
        counts[row[2]] = int(row[0])
    require(set(counts) == {'zfs_dctx_bench:decode', 'zfs_dctx_bench:create'},
            'Incomplete diagnostic event set')
    return counts


def write_event(text):
    fd = os.open('/sys/kernel/tracing/kprobe_events', os.O_WRONLY)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def validate_case(args, spec, cell, item, context, command, out):
    out.mkdir(parents=True, exist_ok=True)
    new_case(out, cell['id'])
    name = out / cell['id']
    record = dict(cell, context=context, valid=False)
    installed = []
    try:
        check_files(item, cell['workers'])
        with cache_control(cell['cache']):
            warm(command, name, spec)
            try:
                for event, function in (
                        ('decode', 'zfs_zstd_decompress_level_buf'),
                        ('create', 'zfs_ZSTD_createDCtx_advanced')):
                    write_event('p:zfs_dctx_bench/{} {}\n'.format(
                        event, function))
                    installed.append(event)
                before = stats()
                run(['perf', 'stat', '-a', '-x,', '-o',
                     str(name) + '-events.csv', '-e',
                     'zfs_dctx_bench:decode,zfs_dctx_bench:create', '--']
                    + command + ['--time_based=1',
                                 '--runtime=' + str(spec['seconds']),
                                 '--output=' + str(name) + '.json'])
                delta = {k: v - before[k] for k, v in stats().items()}
            finally:
                with cleanup_signals():
                    errors = []
                    for event in reversed(installed):
                        try:
                            write_event('-:zfs_dctx_bench/{}\n'.format(event))
                        except OSError as error:
                            errors.append(str(error))
                    require(not errors, 'Probe cleanup failed: ' + str(errors))
        data = json.loads(Path(str(name) + '.json').read_text())
        reads, expected = fio_reads(data, cell, spec)
        create, reuse = check_delta(delta, cell['cache'], cell['corpus'],
                                    expected)
        counts = perf_counts(Path(str(name) + '-events.csv'))
        require(counts['zfs_dctx_bench:decode'] == expected
                and counts['zfs_dctx_bench:create'] == create,
                'Decoder/creation probes disagree with fio and kstats')
        record.update(valid=True, reads=reads, expected_decodes=expected,
                      counts=counts, delta=delta,
                      fio_sha256=digest(Path(str(name) + '.json')),
                      events_sha256=digest(Path(str(name) + '-events.csv')))
    except Exception as error:
        record['failure'] = str(error)
        raise
    finally:
        save(Path(str(name) + '.meta.json'), record)
    print(cell['id'], 'sustained decoder validation passed', flush=True)


def validate(args):
    spec, cell, item, context, command = case_context(args)
    validate_case(args, spec, cell, item, context, command, args.output)


def check_validation(directory, cell, spec, context):
    name = directory / cell['id']
    meta = json.loads(Path(str(name) + '.meta.json').read_text())
    require(meta['valid'] and meta['context'] == context
            and all(meta[k] == v for k, v in cell.items()),
            'Missing, stale or mismatched diagnostic validation')
    require(digest(Path(str(name) + '.json')) == meta['fio_sha256']
            and digest(Path(str(name) + '-events.csv'))
            == meta['events_sha256'],
            'Diagnostic raw evidence changed')
    data = json.loads(Path(str(name) + '.json').read_text())
    reads, expected = fio_reads(data, cell, spec)
    create, reuse = check_delta(meta['delta'], cell['cache'], cell['corpus'],
                                expected)
    counts = perf_counts(Path(str(name) + '-events.csv'))
    require(counts == meta['counts'] and reads == meta['reads']
            and expected == meta['expected_decodes']
            and counts['zfs_dctx_bench:decode'] == expected
            and counts['zfs_dctx_bench:create'] == create,
            'Diagnostic decoder accounting mismatch')
    return digest(Path(str(name) + '.meta.json'))


def measure(args):
    spec, cell, item, context, command = case_context(args)
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    new_case(out, cell['id'])
    diagnostic = out / 'validation'
    validate_case(args, spec, cell, item, context, command, diagnostic)
    # Recheck the loaded module, controls and CPUs after removing probes.
    require(case_context(args)[3] == context,
            'Conditions changed after validation')
    proof = check_validation(diagnostic, cell, spec, context)
    name = out / cell['id']
    record = dict(cell, context=context, validation_sha256=proof, valid=False)
    try:
        with cache_control(cell['cache']):
            warm(command, name, spec)
            before = stats()
            record['proc_stat_before'] = Path('/proc/stat').read_text()
            record['started'] = time.time()
            record['monotonic_started'] = time.monotonic()
            timed = command + ['--time_based=1',
                               '--runtime=' + str(spec['seconds']),
                               '--output=' + str(name) + '.json']
            run(timed)
            record['monotonic_finished'] = time.monotonic()
            record['finished'] = time.time()
            record['proc_stat_after'] = Path('/proc/stat').read_text()
            after = stats()
        record['kstat_before'], record['kstat_after'] = before, after
        delta = {k: after[k] - before[k] for k in before}
        data = json.loads(Path(str(name) + '.json').read_text())
        reads, expected = fio_reads(data, cell, spec)
        check_delta(delta, cell['cache'], cell['corpus'], expected)
        check_files(item, cell['workers'])
        require(case_context(args)[3] == context,
                'Conditions changed while timing')
        record.update(delta=delta, command=timed, reads=reads,
                      expected_decodes=expected,
                      fio_sha256=digest(Path(str(name) + '.json')), valid=True)
    except Exception as error:
        record['failure'] = str(error)
        raise
    finally:
        save(Path(str(name) + '.meta.json'), record)
    print(cell['id'], 'passed', flush=True)


def fio_metrics(data):
    jobs = data['jobs']
    count = sum(j['read']['total_ios'] for j in jobs)
    bins = {}
    for job in jobs:
        read = job['read']
        require(sum(read['clat_ns']['bins'].values()) == read['total_ios'],
                'Incomplete latency histogram')
        for latency, samples in read['clat_ns']['bins'].items():
            latency = int(latency)
            require(latency > 0 and samples >= 0, 'Invalid latency histogram')
            bins[latency] = bins.get(latency, 0) + samples
    tails = {}
    for percentile in (50, 95, 99):
        cumulative = 0
        for latency, samples in sorted(bins.items()):
            cumulative += samples
            if cumulative >= math.ceil(count * percentile / 100):
                tails['p{}_us'.format(percentile)] = latency / 1000
                break
    bandwidth = sum(j['read']['io_bytes'] * 1000 / j['read']['runtime']
                    for j in jobs) / 1024**2
    metrics = dict(tails, mib_s=bandwidth,
                   mean_read_us=sum(j['read']['lat_ns']['mean'] *
                                    j['read']['total_ios'] for j in jobs)
                   / count / 1000,
                   summed_user_cpu_pct=sum(j['usr_cpu'] for j in jobs),
                   summed_system_cpu_pct=sum(j['sys_cpu'] for j in jobs),
                   context_switches=sum(j['ctx'] for j in jobs))
    require(all(math.isfinite(v) and v > 0 for k, v in metrics.items()
                if k in ('mib_s', 'mean_read_us', 'p50_us', 'p95_us',
                         'p99_us')),
            'Nonpositive or nonfinite throughput/latency')
    return metrics


def matching_conditions(a, b, same_build):
    left, right = a['context'], b['context']
    for key in ('fixture_sha256', 'fixture_properties', 'parameters', 'kernel',
                'boot_id', 'cpus', 'placement', 'fio_version', 'pool_layout',
                'pool_features', 'runner_sha256'):
        require(left[key] == right[key],
                'Comparison conditions differ: ' + key)
    x, y = left['build_identity'], right['build_identity']
    spl_keys = ('sha256', 'note_sha256', 'srcversion')
    require(x['compiler'] == y['compiler']
            and x['configure'] == y['configure']
            and x['compile_commands'] == y['compile_commands']
            and all(x['modules']['spl'][k] == y['modules']['spl'][k]
                    for k in spl_keys),
            'Comparison release configuration/compiler/SPL differs')
    if same_build:
        require(x == y, 'Same-build comparison used different binaries')
    else:
        require(x['modules']['zfs']['sha256'] != y['modules']['zfs']['sha256'],
                'Cross-build controls used the same ZFS binary')


def report(args):
    spec = json.loads(args.plan.read_text())
    require(spec['schema'] == 3, 'Regenerate the plan with this runner')
    expected = {c['id'] for c in spec['cases']}
    actual = {p.name.removesuffix('.meta.json')
              for p in args.results.glob('*.meta.json')}
    require(actual == expected and len(expected) == len(spec['cases']),
            'Missing, duplicate or unexpected cases')
    rows, previous = [], None
    identities = {}
    for cell in spec['cases']:
        name = cell['id']
        meta = json.loads(
            (args.results / (name + '.meta.json')).read_text())
        require(meta['valid']
                and all(meta[k] == v for k, v in cell.items()),
                name + ': invalid measurement or case metadata')
        context = meta['context']
        require(context['plan_sha256'] == digest(args.plan)
                and context['build_identity']['build'] == cell['build'],
                name + ': plan/build mismatch')
        identity = context['build_identity']
        require(identities.setdefault(cell['build'], identity) == identity,
                'A build label changed identity during the experiment')
        proof = check_validation(
            args.results / 'validation', cell, spec, context)
        require(proof == meta['validation_sha256'],
                'Validation evidence changed')
        require(digest(args.results / (name + '.json')) == meta['fio_sha256'],
                'Timed raw evidence changed')
        require(all(math.isfinite(meta[k]) for k in (
            'started', 'finished', 'monotonic_started', 'monotonic_finished'))
            and meta['monotonic_finished'] > meta['monotonic_started'],
            'Invalid execution timestamps')
        if previous is not None:
            require(previous['context']['boot_id'] == context['boot_id']
                    and previous['monotonic_finished']
                    <= meta['monotonic_started'],
                    'Cases overlapped or did not follow the planned order')
        previous = meta
        data = json.loads((args.results / (name + '.json')).read_text())
        reads, expected_decodes = fio_reads(data, cell, spec)
        elapsed = meta['monotonic_finished'] - meta['monotonic_started']
        require(all(j['read']['runtime'] <= elapsed * 1000 + 1
                    for j in data['jobs']),
                'fio duration exceeds its interval')
        delta = {k: meta['kstat_after'][k] - v
                 for k, v in meta['kstat_before'].items()}
        require(delta == meta['delta'] and reads == meta['reads']
                and expected_decodes == meta['expected_decodes'],
                'Timed decoder accounting mismatch')
        create, reuse = check_delta(delta, cell['cache'], cell['corpus'],
                                    expected_decodes)
        rows.append(dict(cell, **fio_metrics(data), context=context,
                         started=meta['started'], finished=meta['finished'],
                         monotonic_started=meta['monotonic_started'],
                         monotonic_finished=meta['monotonic_finished'],
                         create=create, reuse=reuse,
                         reuse_fraction=(reuse / (create + reuse)
                                         if create + reuse else None)))
    groups = {}
    for row in rows:
        key = (row['build'], row['cache'], row['corpus'], row['level'],
               row['block'], row['workers'])
        groups.setdefault(key, []).append(row)
    comparisons = []
    for key, group in sorted(groups.items()):
        build, cache, corpus, level, block, workers = key
        references = []
        if cache != 0:
            references.append((build, 0, corpus, level, block, workers))
        if build == 'reuse' and cache == 16:
            references.append(('baseline', 0, corpus, level, block, workers))
        if build == 'hoisted-reuse' and cache == 16:
            references.append(('hoisted-baseline', 0, corpus, level, block,
                               workers))
        if build == 'hoisted-baseline':
            references.append(('baseline', 0, corpus, level, block, workers))
        if build == 'hoisted-reuse':
            references.append(('reuse', cache, corpus, level, block, workers))
        if build == 'reuse' and cache == 0:
            references.append(('baseline', 0, corpus, level, block, workers))
        if build == 'hoisted-reuse' and cache == 0:
            references.append(('hoisted-baseline', 0, corpus, level, block,
                               workers))
        for ref in references:
            if ref not in groups:
                continue
            a = {r['round']: r for r in groups[ref]}
            b = {r['round']: r for r in group}
            require(set(a) == set(b) == set(range(1, spec['rounds'] + 1)),
                    'Incomplete paired comparison')
            gaps = []
            for repeat in sorted(a):
                matching_conditions(a[1], a[repeat], True)
                matching_conditions(a[repeat], b[repeat], build == ref[0])
                gap = abs(b[repeat]['monotonic_started'] -
                          a[repeat]['monotonic_started'])
                require(gap <= spec['max_pair_gap'],
                        'Paired measurements are too far apart')
                gaps.append(gap)
            gains = [100 * (b[r]['mib_s'] / a[r]['mib_s'] - 1)
                     for r in sorted(a)]
            tail_gains = {metric: [
                100 * (b[r][metric] / a[r][metric] - 1) for r in sorted(a)]
                for metric in ('mean_read_us', 'p95_us', 'p99_us')}
            comparisons.append(dict(reference=ref, candidate=key,
                                    paired_gain_pct=gains,
                                    paired_start_gap_seconds=gaps,
                                    median_gain_pct=statistics.median(gains),
                                    paired_latency_change_pct=tail_gains,
                                    candidate_median_mib_s=statistics.median(
                                        r['mib_s'] for r in group),
                                    candidate_cv_pct=100 * statistics.stdev(
                                        r['mib_s'] for r in group)
                                    / statistics.mean(
                                        r['mib_s'] for r in group)))
    args.output.mkdir()
    large = [c for c in comparisons if c['candidate'][4] == 1048576]
    summary = dict(comparisons=comparisons, cases=len(rows),
                   one_mib_comparisons=large)
    save(args.output / 'summary.json', summary)
    with (args.output / 'measurements.csv').open('x') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with (args.output / 'one-mib.md').open('x') as stream:
        stream.write('# 1 MiB paired results\n\n'
                     'Negative throughput changes indicate regressions; '
                     'positive latency changes indicate slower reads. '
                     'Ranges are individual pairs, not confidence intervals. '
                     'Incompressible rows are controls without decoding.\n\n'
                     '| Candidate / reference | Corpus | Level | Workers | '
                     'Median throughput % | Pair range % | '
                     'Median p95 change % | Median p99 change % |\n'
                     '| --- | --- | --- | --- | --- | --- | --- | --- |\n')
        for comparison in large:
            build, cache, corpus, level, _, workers = comparison['candidate']
            ref = comparison['reference']
            gains = comparison['paired_gain_pct']
            tails = comparison['paired_latency_change_pct']
            stream.write('| {} c{} / {} c{} | {} | {} | {} | {:+.2f} | '
                         '{:+.2f} to {:+.2f} | {:+.2f} | {:+.2f} |\n'.format(
                             build, cache, ref[0], ref[1], corpus, level,
                             workers, comparison['median_gain_pct'],
                             min(gains), max(gains),
                             statistics.median(tails['p95_us']),
                             statistics.median(tails['p99_us'])))
    print('{} cases, {} comparisons; ranges are not confidence intervals'
          .format(len(rows), len(comparisons)))


def batch(args):
    spec = json.loads(args.plan.read_text())
    require(len({c['build'] for c in spec['cases']}) == 1,
            'Batch requires a single-build plan; switch builds externally')
    verify(args)
    for cell in spec['cases']:
        args.case = cell['id']
        measure(args)
    verify(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    p = actions.add_parser('plan')
    p.add_argument('--workers', type=int, nargs='+', required=True)
    p.add_argument('--physical-workers', type=int)
    p.add_argument('--study', choices=['sweep', 'controls'], default='sweep')
    p.add_argument('--max-pair-gap', type=int, default=600)
    p.add_argument('--builds', nargs='+', choices=BUILDS)
    p.add_argument('--levels', nargs='+',
                   choices=['fast', '3', '7', '19'])
    p.add_argument('--corpora', nargs='+', default=list(CORPORA),
                   choices=CORPORA)
    p.add_argument('--rounds', type=int, default=5)
    p.add_argument('--seconds', type=int, default=10)
    p.add_argument('--ramp', type=int, default=2)
    p.add_argument('--seed', type=int, default=111)
    p.add_argument('--output', type=Path, required=True)
    p = actions.add_parser('verify')
    p.add_argument('--fixture', type=Path, required=True)
    p = actions.add_parser('prepare')
    p.add_argument('--dataset', required=True)
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    for action in ('measure', 'validate', 'batch'):
        p = actions.add_parser(action)
        p.add_argument('--plan', type=Path, required=True)
        p.add_argument('--fixture', type=Path, required=True)
        if action != 'batch':
            p.add_argument('--case', required=True)
        p.add_argument('--build-manifest', type=Path, required=True)
        p.add_argument('--cpus', required=True)
        p.add_argument('--placement', type=Path, required=True)
        p.add_argument('--output', type=Path, required=True)
    p = actions.add_parser('report')
    p.add_argument('--plan', type=Path, required=True)
    p.add_argument('--results', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p = actions.add_parser('manifest')
    p.add_argument('--build', choices=BUILDS, required=True)
    p.add_argument('--module', action='append', required=True)
    p.add_argument('--config-status', type=Path, required=True)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--compiler', required=True)
    p.add_argument('--output', type=Path, required=True)
    p = actions.add_parser('instrument-baseline')
    p.add_argument('--caller', type=Path, required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    globals()[args.action.replace('-', '_')](args)
    require(not CANCELLED, 'Benchmark interrupted')


if __name__ == '__main__':
    main()

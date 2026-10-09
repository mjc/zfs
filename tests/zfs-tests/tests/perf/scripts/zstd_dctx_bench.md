# DCtx benchmark protocol

This suite measures real ZFS kernel reads, including the caller, allocator and
context cache. It requires Python 3.9+, fio JSON+ support, perf, and a disposable
Linux KVM guest with a non-ZFS root and at least four online CPUs. FreeBSD,
native kernel measurements and large NUMA systems need separate coverage.

ZTS owns its disposable pool; the Python runner uses an existing dataset and
never destroys pools or reloads modules. Verify disposable devices before ZTS.
Check host load before booting a guest; leave it off while the host is busy and
shut it down when it is not in use. Use the repository devenv workflow.

## Two separate studies

The `sweep` study uses one candidate binary, with cache off/on across levels
`zstd-fast`, 3, 7 and 19, 4 KiB/128 KiB/1 MiB records, all four corpora, and
one/verified physical-core-count/all-requested-thread readers. Its level-3
saturation matrix uses 4 KiB/1 MiB records, every worker count and capacities
0/2/4/8/16. If physical placement is unknown, omit `--physical-workers`; the
plan records it as unknown and uses the endpoint counts. Never substitute the
number of guest CPUs for a measured physical-core count.

The smaller `controls` study uses level 3, all record sizes/corpora and endpoint
worker counts with six conditions:

| Build label | Context code | CPU probing | Cache |
| --- | --- | --- | --- |
| baseline | Before initialized DCtx reuse | Repeated CPUID | 0 |
| reuse | Candidate | Repeated CPUID | 0/16 |
| hoisted-baseline | Before initialized DCtx reuse | Detect once | 0 |
| hoisted-reuse | Candidate | Detect once | 0/16 |

Run `instrument-baseline --caller path/to/module/zstd/zfs_zstd.c` on
**both baseline source snapshots** before building. It adds the
same successful fresh-context creation counter as the disabled candidate, plus
a reuse counter that remains zero. This is accounting instrumentation for
benchmark builds only; it does not add reuse or change vendored Zstd. The timed
baseline is therefore an instrumented control, not an untouched binary. All
four builds perform one context-accounting increment per decode. The runner
refuses builds lacking these counters; it never infers decoding from fio alone.

Use matching explicit `--disable-debug --disable-debuginfo` release configure
arguments, compiler, kernel, SPL and tools. `--enable-debuginfo` changes inlining.
For the hoisted experimental snapshots, check creation disassembly for no CPUID
instructions; original snapshots should retain them. Preserve that evidence and
the source patch. This suite does not make the hoisting change itself.

Five rounds are the default. Each complete workload's conditions are adjacent;
condition order rotates and reverses, workload order is seeded and shuffled.
The control matrix is not mixed into the lengthy capacity sweep. Follow the
recorded order when switching modules. Analysis rejects overlap, order changes,
reboots and paired monotonic starts more than `--max-pair-gap` seconds apart (default 600).
It also reports each actual pair gap. If switching cannot meet the bound, fix
the execution protocol or choose and justify a bound before generating the
plan. Do not silently relax it afterwards or discard inconvenient samples.

## Workload and decoder accounting

Each worker reads a distinct deterministic 18 MiB file. The corpora incorporate
the data variety used in [Adam D. Moss's PR #11709](https://github.com/openzfs/zfs/pull/11709):

| Corpus | Record contents | Expected decoder calls |
| --- | --- | --- |
| high | Repeated nonzero 32-byte pattern, varying by record | Every read |
| moderate | Patterned first half, pseudorandom second half | Every read |
| incompressible | Pseudorandom bytes throughout | Zero |
| mixed | Repeating high/moderate/incompressible records | Two per three reads |

Mixed files contain a multiple of three records. For a worker ending partway
through a loop, the exact expected count is `2*(reads//3)+min(reads%3,2)`.
Separate per-worker fio JSON+ records retain this information. Every worker must
read at least two complete files in both the sustained diagnostic and timed
runs. Histograms are merged by sample count for aggregate completion-latency
p50/p95/p99; mean total read latency is weighted by completed I/O. CPU percentages
are summed across workers, not described as one worker's utilization.

Fixtures are written once, unchanged across builds. Preparation rejects
compression/allocation failures and records properties, compression ratios and
SHA-256 checksums. Dedup, atime and secondary cache are disabled. Each case checks
its file checksums before diagnostic validation and after timing. Read size
matches record size. The generator is synthetic, not representative application
data; measured ratios and decoder counts test its actual storage behavior.

The pool must have `feature@embedded_data=disabled` and an ashift=9 layout
on disposable 512-byte devices (file vdevs are suitable). Embedded records
bypass compressed ARC and its miss accounting; on ashift=12 pools, non-embedded
4 KiB records cannot retain compression after allocation-size rounding. The
runner checks the actual feature state and vdev ashifts; use new fixtures if
either changes. The wrapper rejects block devices with other logical sectors.

Require `dbuf_cache_max_bytes=0`, `zfs_prefetch_disable=1` and compressed ARC on.
Provide enough ARC for the active fixture. This measures forced ARC decompression,
not default-cache disk throughput. All counted intervals require zero ARC demand
data/metadata misses, decoder/allocation errors and reserved-memory fallback.
Busy-context-cache fallback is allowed and counted as fresh creation. Small
capacities need not achieve 99.9% reuse. Incompressible controls must never decode
and cannot establish a DCtx benefit. Raw allocator `zstd.buffers/size` gauges do
not account for all initialized DCtx memory; do not label them as such.

Every `measure` automatically performs a separate sustained diagnostic first,
using the same CPU subset, cache setting and workload. Decoder/creation kprobes
must match per-worker expected reads and context kstats exactly. Successful raw
fio JSON+, perf CSV and metadata are saved under `results/validation/` and bound
to the plan, fixture, build, module incarnation, boot, CPUs and all controls.
Analysis rechecks that raw evidence. Missing/stale/altered diagnostics are fatal.
Standalone `validate` is available for debugging but does not replace the fresh
diagnostic inside `measure`.

A full-file warm pass and a separate time-based warmup precede each counted
invocation. Defaults are two seconds of warmup and ten seconds of sustained
reads. The counted fio invocation has **no ramp**: context counts must equal its
completed I/O layout. This rejects workloads that become cached halfway through.
No profiler runs during timing. After diagnostic probes are removed, the runner
rechecks module identity and conditions before timing. It also requires tracer
`nop` and no other kprobe events; ensure no independent hardware-counter or
sampling profiler is running. Instrumented times must be labeled separately.

## Identity and placement evidence

Create a manifest for each release build, using its actual uncompressed ELF
module artifacts and config.status:

```sh
runner=tests/zfs-tests/tests/perf/scripts/zstd_dctx_bench.py
python3 "$runner" manifest --build hoisted-reuse \
    --source "$source_tree" --config-status "$build_tree/config.status" \
    --compiler "$compiler" --module "zfs=$build_tree/module/zfs.ko" \
    --module "spl=$build_tree/module/spl.ko" --output /var/tmp/build.json
```

The manifest saves module SHA-256, GNU build-ID note hash, srcversion, compiler,
configure arguments, normalized actual Kbuild compile/link commands, source
revision and tracked source patch hash. Keep the Kbuild .cmd files; make-time
flag overrides are part of the comparison identity. Retain
source patches, configure logs and disassembly alongside it. The runner compares
actual loaded GNU notes/srcversion with the recorded artifacts, rather than
comparing a loaded srcversion to itself. Each build label must retain one identity
throughout the study. Same-build cache comparisons require identical manifests;
cross-build comparisons require distinct ZFS binaries and identical compiler,
configure arguments and SPL. All comparisons require matching fixture/properties,
all other module parameters, kernel, boot, fio, CPU subset, placement and pool
layout. The report retains these conditions with the measurements.

Supply `--placement` as a JSON evidence file, not a descriptive label:

```json
{
  "host": "recorded host identity",
  "vcpu_map": {
    "0": {"host_cpu": 2, "core": 1, "siblings": [2, 8], "numa": 0},
    "1": {"host_cpu": 3, "core": 2, "siblings": [3, 9], "numa": 0}
  },
  "background": "QEMU/emulator and other thread affinity evidence",
  "controls": "saved governor, load, frequency and maintenance controls"
}
```

Include every worker vCPU and actual physical CPU/core/SMT/NUMA mapping, along
with QEMU affinity observations. The runner checks presence and equality of this
operator-supplied evidence; it cannot observe host affinity from inside the guest
or prove that an operator's report is true. Do not claim isolation from a label.
Keep other VM threads off workers' physical cores/siblings at low concurrency;
record unavoidable background work at full occupancy. Preserve host load,
frequency, temperatures, sibling utilization, vCPU wait and guest steal separately.

`--cpus` is a distinct comma-separated list in placement priority order. The
same prefix is used in diagnostics and timing; fio assigns those selected IDs in
numeric order. Both modes reject unavailable/duplicate/insufficient CPUs.

## Running and analysis

Generate separate plans before changing modules or creating fixtures:

```sh
python3 "$runner" plan --study sweep --builds hoisted-reuse \
    --workers 1 2 4 6 8 12 --physical-workers 6 \
    --output /var/tmp/sweep-plan.json
python3 "$runner" plan --study controls --levels 3 \
    --workers 1 12 --output /var/tmp/control-plan.json
```

These example counts require corresponding hardware. Add larger available counts
for saturation; oversubscribing twelve vCPUs does not test a larger machine.
Budget storage and time: all corpora multiply the fixture matrix by four. The
planner reports a minimum diagnostic/timed/warmup duration, excluding full warm
passes, checksums and module switches. `--corpora mixed` is a labeled pilot, not
the complete comparison.

Prepare once per plan in a child of the disposable harness pool:

```sh
zpool create -o feature@embedded_data=disabled -o ashift=9 \
    "$PERFPOOL" $disposable_devices
zfs create "$PERFPOOL/dctx"
python3 "$runner" prepare --plan /var/tmp/control-plan.json \
    --dataset "$PERFPOOL/dctx" --output /var/tmp/preparation
```

An external controller follows the plan in order, exports only the test pool,
verifies no other pools are imported, switches the correct modules and imports
the same pool. Apply matching controls and recorded affinity. For each case:

```sh
python3 "$runner" measure --plan /var/tmp/control-plan.json \
    --fixture /var/tmp/preparation/fixture.json --case "$case_id" \
    --build-manifest "$build_manifest" --cpus "$guest_cpu_list" \
    --placement /var/tmp/placement.json --output /var/tmp/results
```

The runner changes/restores only the cache limit. SIGINT/SIGTERM stop and reap
its subprocess group, remove its own probes and restore the original limit.
ZTS stops/reaps the runner before restoring controls and destroying the pool.
Failed cases retain failure metadata and cannot be overwritten; use a new output
path for a complete rerun. Verify all fixtures after build groups and completion.

The ZTS `zstd_decompress` wrapper runs a single-build sweep with automatic
validation. Set `PERF_ZSTD_MANIFEST` and `PERF_ZSTD_PLACEMENT` to evidence files.
Other controls are `PERF_NTHREADS`, optional `PERF_ZSTD_PHYSICAL_WORKERS`,
`PERF_ZSTD_CPUS`, `PERF_ZSTD_BUILD` (default reuse), `PERF_ZSTD_LEVELS`,
`PERF_ZSTD_CORPORA`, `PERF_ZSTD_ROUNDS`, `PERF_ZSTD_RUNTIME` and `PERF_ZSTD_RAMP`.
The `_19` wrapper defaults to level 19. `batch` refuses multiple builds.

```sh
python3 "$runner" verify --fixture /var/tmp/preparation/fixture.json
python3 "$runner" report --plan /var/tmp/control-plan.json \
    --results /var/tmp/results --output /var/tmp/analysis
make check-zstd-dctx-bench
```

Analysis saves CSV samples, paired throughput/latency changes, medians, variation
and pair gaps in JSON. `one-mib.md` includes every 1 MiB comparison, including
regressions, addressing the large-record concern in #11709. Negative throughput
changes or positive latency changes indicate regressions. Pair ranges are not
confidence intervals. Sign changes or variation as large as a tiny gain do not
establish benefit. A small single-node VM cannot establish NUMA/128-core/native
scalability. Preserve raw measurements, controller and evidence for replication.

## Allocation-failure correctness

`make unit T=zstd` tests deterministic 50% and 100% failure of the actual ZFS
caller's nonblocking `vmem_alloc`, following Adam's failure-testing idea. At
128 KiB/1 MiB and levels 1/3/19, sixteen fresh-cache trials check allocation
counts, uncached fallback after failed population, exact decoded bytes and later
population/reuse. Sleeping allocations can succeed. This does not test kernel
memory pressure, reserved-memory exhaustion or concurrent failure. Run it
separately from timing. The Python regression checks exercise invalid evidence,
comparison conditions, decoder accounting, plan order and cancellation locally;
they are not kernel benchmark results.

The auxiliary `zstd` test reports buffered logical write admission, including
possible overwrite coalescing, not completed compression throughput. It does
not prove one compression per fio write. `PERF_ZSTD_PROFILE=1` enables a separate
instrumented diagnostic run; exclude that run from throughput comparisons.

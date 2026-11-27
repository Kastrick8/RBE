# RBE Video Manager

Video Manager is RBE's global media identity, metadata, quarantine, validation, normalization, job-control, and live-session control subsystem. Privileged network/media/process work stays in trusted Rust; REL receives a narrow ownership-scoped control surface.

## Module REL capability

Video Manager is currently a privileged **Module REL** capability. Import it explicitly:

```text
:import[vm]
:import[video-manager as media]
:import[video-manager.status as videoStatus]
```

The legacy name `video` is intentionally not an alias. Route REL cannot import Video Manager directly; put privileged media operations behind a Module REL interface.

Language code does not receive raw database handles, filesystem paths, FFmpeg command lines, or process-spawn primitives.

## Stable asset identity

Assets use generated IDs and an RBE-owned URI shape such as:

```text
vm://module:learning.catalog/tutorials/<asset-id>
```

The URI is a Video Manager identity, not an exposed local path. Module ownership is part of the namespace, and lookups/jobs/variants/live sessions are checked against the calling module's ownership before data is returned.

## Current language API

The current Module facade includes:

```text
status()
databaseHealth([database])
get(assetId[, database])
job(jobId[, database])
variants(assetId[, database])
create(group, title, sourceType[, sourceUri[, metadata[, database]]])
queueDownload(group, title, url[, metadata[, database]])
reserveLive(assetId[, database])
liveSession(sessionId[, database])
endLive(sessionId[, database])
```

Snake-case aliases exist for several multi-word functions.

Public job/variant/session views deliberately omit privileged worker diagnostics, storage paths, and trusted live transport endpoints.

## Download pipeline

Remote downloads enter quarantine rather than going directly to a player/FFmpeg shell path.

The implemented high-level flow is:

```text
queued
  -> downloading
  -> downloaded
  -> inspecting
  -> container_checked
  -> probing
  -> probed
  -> normalizing
  -> ready
```

A failed trusted stage records failure instead of promoting unvalidated bytes as ready media.

The pipeline includes:

- strict URL parsing/normalization;
- DNS resolution and rejection of private/loopback/link-local/special targets;
- redirect revalidation and HTTPS downgrade protection;
- bounded streaming download into quarantine;
- container-signature preflight;
- trusted FFprobe validation;
- trusted FFmpeg normalization;
- atomic metadata promotion of the normalized variant/asset/job;
- quarantine cleanup after successful promotion;
- lazy mother-owned queued-job scheduling and startup recovery.

`create(..., "download")` is rejected; remote downloads must use `queueDownload()` so they cannot bypass quarantine.

## FFprobe/FFmpeg boundary

Configured FFprobe/FFmpeg executables are trusted runtime inputs. Relative configured paths are resolved by RBE; Module REL does not supply executable names or arbitrary switches.

Normalization produces the controlled standard MP4 profile with H.264 video, AAC audio, `yuv420p`, first video/optional first audio stream, subtitles/data excluded, local/data protocol restrictions, stdin disabled, bounded logs/timeouts, and no overwrite of an existing output.

## Hardware video encoding — implemented with fallback

At worker startup, RBE probes FFmpeg capabilities and performs bounded smoke verification for supported hardware H.264 candidates. The backend chooses the first verified typed hardware encoder when available; otherwise it uses `libx264`.

Typed hardware candidates currently include:

- NVIDIA NVENC (`h264_nvenc`);
- Intel Quick Sync (`h264_qsv`);
- AMD AMF (`h264_amf`);
- Apple VideoToolbox (`h264_videotoolbox`);
- Windows Media Foundation (`h264_mf`).

The selected encoder becomes part of trusted `FfmpegPolicy`, not a language-provided switch. If a selected hardware encode fails during normalization, RBE removes the failed output and retries **once** with software `libx264`. If that fallback also fails, the normalization fails normally.

Normalized media metadata records which typed encoder actually succeeded.

## Probe metadata

Validated media probing carries useful stream metadata such as width, height, frame rate, and estimated bitrate into the ready variant metadata where available. Values are bounded/validated before the asset becomes ready.

## Live session control — implemented control plane

Video Manager now has a persisted live-session state machine:

```text
reserved -> starting -> live -> stopping -> ended
        \       \        \
         +------ failed ---+
```

Current manager/language operations can reserve a session, query it, and request its end. Duplicate/invalid transitions are rejected by the state machine rather than silently rewriting state.

Trusted Rust owns transport binding. A trusted runtime can bind:

- RTMP/RTMPS ingest;
- WHIP over HTTPS;
- optional HTTPS playback endpoint.

Endpoint URLs are validated, and credentials/fragments are rejected from these bindings. Module REL **does not receive the ingest/playback endpoint strings** through the public live-session view and cannot self-promote a reservation into a trusted active transport.

### Important live-media limit

The live **control plane/state/binding contract** is implemented. Do not infer from that that RBE already contains a complete production RTMP/HLS/WHIP media server, transcoder, CDN, or WebRTC stack. Actual live transport/runtime workers and serving behavior remain separate implementation work where not already wired by trusted callers.

## Database model

The built-in default adapter uses SQLite with WAL/foreign-key enforcement. Video Manager stores namespace/group/asset/variant/job/live-session metadata and resolves database adapters by explicit name.

Unknown database adapters fail closed instead of silently falling back to `default`.

Ready promotion is transactional at the metadata layer: the normalized variant, asset-ready transition, and job-ready transition are committed together. If promotion fails, the staged/promoted output is cleaned rather than leaving metadata that claims a broken asset is ready.

## Lazy worker behavior

The download worker is event-driven rather than a hot polling loop:

1. `queueDownload()` persists the quarantined asset/job;
2. the manager wakes the worker;
3. the worker claims queued work and runs the trusted pipeline;
4. it sleeps again when the queue is empty;
5. bounded recovery scans catch interrupted/restored work.

Worker health/degraded state contributes to backend health reporting when the Video Manager worker is enabled.

## Security model

Video Manager's security boundary is based on ownership and trusted Rust mediation:

- Module identity scopes mutable/read operations;
- remote input begins quarantined;
- URL/DNS/redirect targets are revalidated;
- media bytes must pass signature + FFprobe checks before normalization;
- REL cannot supply shell strings or arbitrary FFmpeg flags;
- internal error strings/path details are not returned in public job views;
- storage paths are stripped from public variant views;
- live transport binding is trusted-Rust-only.

## Still incomplete

Video Manager should not yet be described as a complete media platform. Remaining work includes parts of:

- production live transport/runtime execution and serving;
- HLS/output serving/generation;
- broader upload/local/generated ingestion workers;
- richer normalization/profile/variant policy;
- complete backend-configured custom default adapter behavior;
- a Service-to-Mother Video Manager capability channel if Service REL is later allowed to use VM directly.

## Pipeline, Adapter, and Worker Reference

## Asset, job, and variant model

Implemented source types:

- `upload`
- `download`
- `local`
- `generated`
- `live`
- `recorded_live`

Implemented asset states:

- `reserved`
- `quarantined`
- `processing`
- `ready`
- `failed`
- `deleted`

Download jobs expose stage progress. Current aggregate milestones are approximately:

- `queued`: `0.00`
- `downloading`: `0.05`
- `downloaded`: `0.45`
- `container_checked`: `0.55`
- `probed`: `0.70`
- `normalizing`: `0.75`
- `ready`: `1.00`

These are pipeline-stage progress values, not byte-perfect FFmpeg progress percentages.

The normalized download path currently produces a `standard` MP4 variant using H.264 video and AAC audio. The FFmpeg profile is owned by trusted Rust and is not supplied as arbitrary language/process arguments.

## Default database

`VideoManager::open_default` registers the built-in SQLite adapter as `default` and creates both quarantine and normalized-media roots adjacent to the database data directory.

SQLite uses WAL journaling and foreign-key enforcement. The schema contains:

- `video_namespaces`
- `video_groups`
- `video_assets`
- `video_variants`
- `video_jobs`
- `video_live_sessions`

The adapter supports asset creation/lookup, job lookup, queued-job discovery, aggregate queue depth, startup recovery, atomic job claiming/transitions, variant listing, and atomic ready-variant commit.

Final normalized promotion is transactional at the metadata layer: the ready variant is inserted, the asset changes from `quarantined` to `ready`, and the job changes from `normalizing` to `ready` in one SQLite transaction. If that commit fails, the newly promoted media file is removed instead of leaving a falsely-ready asset.

## Database adapters

`VideoDatabase` is the in-process storage adapter contract. Additional adapters can be registered under explicit names.

Database selection is fail-closed. Requesting an unknown adapter returns an error; Video Manager does not silently fall back to `default`.

Worker-oriented adapter hooks have safe no-work defaults, so a custom adapter must explicitly implement queued-job discovery/recovery if it wants the mother-owned download worker to consume its jobs.

The full backend-configured custom-default-adapter path is not complete yet.

### Worker configuration

The relevant `videoManager` settings are:

```json
{
  "downloadWorkerEnabled": false,
  "ffprobeExecutable": "",
  "ffmpegExecutable": "",
  "workerRecoveryScanSecs": 30,
  "downloadMaxBytes": 8589934592
}
```

The worker is disabled by default. When enabled, FFprobe and FFmpeg must be explicitly configured. Relative executable paths are resolved against the backend binary/runtime root; Video Manager does not search the shell `PATH` and does not accept executable paths from module/route input.

At backend shutdown the worker first receives a graceful stop signal. An in-flight pipeline item may finish within `runtime.gracefulShutdownTimeoutMs`. If it exceeds that budget the task is aborted; the next startup's recovery pass re-queues the interrupted job. Retried downloads identity-check and truncate their reserved quarantine file before writing, so stale partial bytes do not contaminate the retry.

## Container inspection

After download, `inspect_download_container` claims `downloaded -> inspecting` and runs a cheap signature gate before expensive probing.

The gate recognizes supported video-container families such as ISO-BMFF/MP4 and MPEG transport streams while rejecting obvious image/audio/FLV/random payloads. Failure deletes the quarantined bytes and records the job failure.

This signature gate is intentionally not treated as authoritative media decoding. FFprobe is the next required stage.

## FFprobe

`probe_download_media` claims `container_checked -> probing` and launches only the configured trusted FFprobe executable.

The FFprobe policy requires an absolute regular-file executable path after runtime-path resolution and bounds both timeout and captured output. The invocation:

- reads only the quarantined local file
- restricts allowed protocols to local/data-oriented protocols
- captures bounded JSON output
- rejects malformed output
- requires at least one valid video stream
- validates dimensions and frame-rate ranges

Success records `probed`. Failure deletes quarantine and records the job as failed.

## FFmpeg normalization

`normalize_download_media` claims `probed -> normalizing` and invokes the configured trusted FFmpeg executable through `FfmpegPolicy`.

The current standard profile is deliberately fixed by RBE:

- MP4 output
- H.264 via `libx264`
- AAC audio when present
- `yuv420p`
- CRF 23
- medium preset
- fast-start MP4 metadata
- first video stream and optional first audio stream
- subtitles/data streams excluded
- local/data protocol whitelist only
- stdin disabled
- output overwrite disabled
- bounded FFmpeg error output
- bounded execution timeout

Module/service code does **not** receive arbitrary FFmpeg switches, shell strings, process spawning, input paths, or output paths.

FFmpeg writes into a private staging file under the generated asset directory. Only after FFmpeg succeeds is that staging file renamed to the final `primary.mp4`; the DB ready-variant transaction then publishes it. Quarantine is removed after that transaction succeeds.

At worker boot, RBE parses the configured FFmpeg encoder table and requires the software `libx264` and AAC encoders used by the standard profile. Advertised H.264 hardware candidates are recorded from a strict RBE allowlist. Directly testable candidates (currently NVENC, Quick Sync, AMF, VideoToolbox, and Media Foundation) then receive a bounded one-frame synthetic smoke test; only encoders that actually initialize and encode successfully are reported as verified. Device-bound candidates such as VAAPI, V4L2 M2M, and RKMPP remain advertised-only until explicit device handling lands.

Hardware acceleration is still not selected for real normalization yet; the current profile intentionally uses deterministic `libx264`. The verified candidate set is the safety foundation for automatic selection rather than trusting FFmpeg's advertised encoder list.

## Backend integration

When enabled, the backend owns one `VideoManager` in shared application state. `/health` includes database health plus download-worker state/queue depth. An unhealthy default database or a degraded download worker contributes to backend health failure.

The language layer receives a narrow `VideoLanguage` facade rather than raw manager/database/filesystem objects.

The worker is optional. With `downloadWorkerEnabled = false`, queueing remains available but jobs stay quarantined/queued until a trusted worker is enabled or another trusted Rust caller processes them.

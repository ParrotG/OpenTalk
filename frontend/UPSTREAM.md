# LiveKit starter provenance

This frontend is adapted from [livekit-examples/agent-starter-react](https://github.com/livekit-examples/agent-starter-react), commit `c5d78a6c381a0ac80b081cf6aeb8ac454d00ca78` (retrieved October 7, 2026).

The upstream MIT license is preserved in `LICENSE`. The following source files are retained or adapted:

- `components/agents-ui/agent-session-provider.tsx`: native session context and room audio renderer.
- `components/agents-ui/agent-audio-visualizer-wave.tsx` and `react-shader-toy.tsx`: WebGL waveform.
- `hooks/agents-ui/use-agent-audio-visualizer-wave.ts`: agent-state and audio-volume animation; added cancellation of previous animations and cleanup on unmount.
- `lib/shadcn/utils.ts` and `tsconfig.json`: small class-name helper and TypeScript configuration.

The app follows the starter's `useSession`, `useAgent`, and `useSessionMessages` pattern. Cloud sandbox fallback, avatars, camera/screen sharing, themes, marketing screens, markdown chat, and booking controls are omitted. The transcript and persisted-session lifecycle are implemented for OpenTalk's independent control API.

Selected SDK/runtime versions are pinned to the upstream lockfile resolutions. The existing Python lockfile is unchanged. Browser test tooling is an additional development dependency. No upstream dependency upgrades were introduced.

The visualizer responds to audio volume and agent state. It is an animated waveform, not a calibrated PCM measurement instrument.

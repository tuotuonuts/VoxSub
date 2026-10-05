"""Frozen isolated CPU speech worker entry; no GUI or audio playback."""
from voxsub.speech_worker import main
if __name__ == '__main__':
    raise SystemExit(main())

import sys

from meeting_translate.app import main

if __name__ == "__main__":
    if "--tts-worker" in sys.argv:
        # 打包版没有可复用的解释器：让同一个 exe 以 TTS sidecar 模式自启
        from meeting_translate.tts_worker import main as worker_main

        args = sys.argv[sys.argv.index("--tts-worker") + 1 :]
        raise SystemExit(worker_main(args))
    main()

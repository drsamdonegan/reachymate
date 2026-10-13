"""Launch the SDK daemon with its bundled macOS runtime available."""


def main():
    from reachy_mini.daemon.app.main import main as daemon_main

    daemon_main()


if __name__ == "__main__":
    main()

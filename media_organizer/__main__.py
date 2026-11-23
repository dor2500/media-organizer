import sys

from media_organizer.cli import main as cli_main


def main():
    if len(sys.argv) > 1:
        sys.exit(cli_main())
    from media_organizer.gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()

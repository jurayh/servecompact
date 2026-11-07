"""Run the proxy: python -m servecompact [--host H] [--port P]"""

import argparse

from .server import serve


def main():
    parser = argparse.ArgumentParser(prog="servecompact")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    serve(host=args.host, port=args.port)


if __name__ == "__main__":
    main()

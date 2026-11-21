from __future__ import annotations

import argparse

import uvicorn

from tests.mock_api.app import MockTargetState, create_mock_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run offline Mock Target API server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--token", default="test-team-key", help="Expected Bearer token (empty to disable)")
    parser.add_argument("--no-seed-images", action="store_true", help="Do not pre-seed sample mock images")
    args = parser.parse_args()

    state = MockTargetState(expected_token=args.token)
    if not args.no_seed_images:
        state.seed_sample_images()
    app, _ = create_mock_app(state)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

"""Durable PostgreSQL/SQLite queue worker: python -m app.worker [--once]."""
import argparse
import logging
import time

from app.config import Settings
from app.service import RecommendationService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    service = RecommendationService(Settings())
    logging.basicConfig(level=logging.INFO)
    try:
        while True:
            worked = service.process_one_job()
            if args.once:
                break
            if not worked:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        service.storage.close()


if __name__ == "__main__":
    main()

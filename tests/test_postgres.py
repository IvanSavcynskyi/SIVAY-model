"""Real PostgreSQL integration in CI, isolated to a fresh temporary schema."""
import os
from uuid import uuid4

import pytest
from sqlalchemy.schema import CreateSchema, DropSchema

from app.storage.database import Storage
from test_contracts_storage import request


@pytest.mark.skipif(not os.getenv("SIVAY_TEST_POSTGRES_URL"), reason="PostgreSQL URL not configured")
def test_postgres_durable_queue_and_jsonb():
    storage = Storage(os.environ["SIVAY_TEST_POSTGRES_URL"])
    schema = "test_" + uuid4().hex
    with storage.engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    storage.engine = storage.engine.execution_options(schema_translate_map={None: schema})
    try:
        storage.initialize()
        job = storage.create_job(request())
        claimed = storage.claim_job()
        assert claimed[0] == job.job_id
        assert storage.claim_job() is None
        assert storage.finish_job(job.job_id, claimed[1], snapshot_id="snap_postgres")
        assert storage.get_job(job.job_id).status == "completed"
    finally:
        with storage.engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        storage.close()

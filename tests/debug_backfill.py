import asyncio
import traceback
from core.config import settings
from infra.backfill_enqueuer import BackfillEnqueuer

async def main():
    conn = settings.storage_account_connection_string or settings.storage_connection_string
    print('connection_string_present=', bool(conn))
    print('container=', settings.storage_container_incoming)
    print('queue=', settings.document_processing_queue_name)
    enq = BackfillEnqueuer(connection_string=conn, container_name=settings.storage_container_incoming, queue_name=settings.document_processing_queue_name)
    try:
        res = await enq.enqueue_existing(prefix=None, max_items=5, dry_run=True, only_pdf=True)
        print('RESULT', res)
    except Exception as e:
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(main())

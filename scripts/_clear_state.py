"""Script temporaneo: svuota DocumentRegistry e coda document-processing."""
import os

from azure.data.tables import TableServiceClient
from azure.storage.queue import QueueClient

CONN = os.environ["AzureWebJobsStorage"]

# 1. Svuota DocumentRegistry
svc = TableServiceClient.from_connection_string(CONN)
tbl = svc.get_table_client("DocumentRegistry")
entities = list(tbl.list_entities())
print(f"DocumentRegistry: {len(entities)} entita trovate")
for e in entities:
    tbl.delete_entity(partition_key=e["PartitionKey"], row_key=e["RowKey"])
print("DocumentRegistry svuotata")

# 2. Svuota coda
q = QueueClient.from_connection_string(CONN, "document-processing")
q.clear_messages()
print("Coda document-processing svuotata")

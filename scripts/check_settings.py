from core.config import settings

def _b(v):
    return bool(v) if v is not None else False

print('azure_openai_endpoint=', settings.azure_openai_endpoint)
print('azure_openai_key=', _b(settings.azure_openai_key))
print('azure_search_api_key=', _b(settings.azure_search_api_key))
print('storage_account_connection_string (field)=', _b(settings.storage_account_connection_string))
print('storage_connection_string (property)=', bool(settings.storage_connection_string))
print('subscription_id=', settings.azure_subscription_id)

print('\nSummary (selected config):')
print('storage_account_url=', settings.storage_account_url)
print('search_endpoint=', settings.search_endpoint)
print('document_search_index_name=', settings.document_search_index_name)

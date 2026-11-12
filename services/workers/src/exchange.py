import ccxt
import os

def get_exchange_client():
    exchange_id = os.getenv("EXCHANGE_ID", "indodax")
    exchange_class = getattr(ccxt, exchange_id)
    
    client = exchange_class({
        'apiKey': os.getenv("EXCHANGE_API_KEY"),
        'secret': os.getenv("EXCHANGE_SECRET_KEY"),
        'enableRateLimit': True,
    })
    return client

import asyncio
import aiohttp
import json
import dateutil.parser
import time
import sys
from logger import logger
from kafka import KafkaProducer
import yaml
import boto3
from CWMetrics import CWMetrics

# Parse config
with open(sys.argv[1], 'r') as config_file:
    config = yaml.load(config_file)

metrics = CWMetrics(config['exchange']['name'])

kafka_producer = KafkaProducer(bootstrap_servers=config['kafka']['address'], value_serializer=lambda v: json.dumps(v).encode('utf-8'))

delay = config['exchange']['rate_limit'] / int(config['proxy']['ip_pool_size'])

def getSSMParam(ssm,paramName):
    return ssm.get_parameter(Name=paramName, WithDecryption=True)['Parameter']['Value']  

def getOandaCredentials():
    # Read parameters from AWS SSM         
    ssm = boto3.client('ssm', region_name='eu-west-1')

    return {
            'accountid' : getSSMParam(ssm, '/prod/exchange/oanda/accountid'),
            'apikey' : getSSMParam(ssm, '/prod/exchange/oanda/apikey')
            }

async def pollForex(symbols, authkey, accountid):
    i = 0
    while True:
        symbol = symbols[i % len(symbols)]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        url="https://api-fxpractice.oanda.com/v3/accounts/" + accountid + "/pricing",
                        headers={'Authorization': ('Bearer ' + authkey)},
                        params='instruments=' + symbol,
                        proxy=config['proxy']['address']) as resp:
                    yield (await resp.json())
        except Exception as error:
            logger.error("Error while fetching forex rates from Oanda: " + type(error).__name__ + " " + str(error.args))
            metrics.putError()
            
        i += 1
        await asyncio.sleep(delay)

async def forexPoller(symbols, authkey, accountid, orderbookAnalyser):
    async for ticker in pollForex(symbols=symbols, authkey=authkey, accountid=accountid):
        try:
            symbolBase = ticker['prices'][0]['instrument'].split("_")[0]
            symbolQuote = ticker['prices'][0]['instrument'].split("_")[1]
            asks = ticker['prices'][0]['asks']
            bids = ticker['prices'][0]['bids']
            payload = {}
            payload['exchange'] = "oanda"
            payload['symbol'] = symbolBase + "/" + symbolQuote
            payload['data'] = {}
            payload['data']['asks'] = [[float(asks[0]['price']), asks[0]['liquidity']]]
            payload['data']['bids'] = [[float(bids[0]['price']), bids[0]['liquidity']]]
            dt = dateutil.parser.parse(ticker['time'])
            payload['timestamp'] = int(time.mktime(dt.timetuple()) * 1000 + dt.microsecond / 1000)
            
            p = json.dumps(payload, separators=(',', ':'))
            kafka_producer.send(config['kafka']['topic'], p)

            logger.info("Received " + symbolBase+"/" + symbolQuote + " prices from Oanda")
            metrics.put(payload['timestamp'])

        except Exception as error:
            logger.error("Error interpreting Oanda ticker: " + type(error).__name__ + " " + str(error.args))
            metrics.putError()

oandaCredentials = getOandaCredentials()
asyncio.ensure_future(
    forexPoller(
        symbols=config['exchange']['symbols'],
        authkey=oandaCredentials['apikey'],
        accountid=oandaCredentials['accountid'],
        orderbookAnalyser=None))
loop = asyncio.get_event_loop()      
loop.run_forever()

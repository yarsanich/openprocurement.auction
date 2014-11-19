# -*- coding: utf-8 -*-
import argparse
import logging
import requests
import iso8601
import couchdb
import json
import sys

from copy import deepcopy
from datetime import timedelta, datetime
from pytz import timezone
from gevent.event import Event
from gevent.coros import BoundedSemaphore
from apscheduler.schedulers.gevent import GeventScheduler
from .server import run_server
from .utils import (
    sorting_by_amount,
    get_latest_bid_for_bidder,
    sorting_start_bids_by_amount
)

from .templates import (
    INITIAL_BIDS_TEMPLATE,
    PAUSE_TEMPLATE,
    BIDS_TEMPLATE,
    ANNOUNCEMENT_TEMPLATE,
    generate_bids_stage
)
from gevent import monkey

monkey.patch_all()

ROUNDS = 3
FIRST_PAUSE_SECONDS = 300
PAUSE_SECONDS = 120
BIDS_SECONDS = 120

BIDS_KEYS_FOR_COPY = (
    "bidder_id",
    "amount",
    "time"
)

SCHEDULER = GeventScheduler()
SCHEDULER.timezone = timezone('Europe/Kiev')

logging.basicConfig(level=logging.DEBUG,
                    format='%(levelname)s-[%(asctime)s]: %(message)s')


class Auction(object):
    """docstring for Auction"""
    def __init__(self, auction_doc_id, host='', port=8888,
                 database_url='http://localhost:9000/auction',
                 auction_data={}):
        super(Auction, self).__init__()
        self.host = host
        self.port = port
        self.auction_doc_id = auction_doc_id
        self.tender_url = 'http://api-sandbox.openprocurement.org/api/0.3/tenders/{0}/auction'.format(auction_doc_id)
        self._auction_data = auction_data
        self._end_auction_event = Event()
        self.bids_actions = BoundedSemaphore()
        self.database_url = database_url
        self._bids_data = {}
        self.db = couchdb.client.Database(self.database_url)

    def get_auction_document(self):
        self.auction_document = self.db.get(self.auction_doc_id)

    def save_auction_document(self):
        self.db.save(self.auction_document)

    def add_bid(self, round_id, bid):
        if round_id not in self._bids_data:
            self._bids_data[round_id] = []
        self._bids_data[round_id].append(bid)

    def get_round_number(self, stage):
        for index, end_stage in enumerate(self.rounds_stages):
            if stage < end_stage:
                return index
        return ROUNDS

    def get_round_stages(self, round_num):
        return (round_num * (self.bidders_count + 1) - self.bidders_count,
                round_num * (self.bidders_count + 1), )

    def filter_bids_keys(self, bids):
        filtered_bids_data = []
        for bid_info in bids:
            bid_info = {key: bid_info[key] for key in BIDS_KEYS_FOR_COPY}
            bid_info["bidder_name"] = self.mapping[bid_info['bidder_id']]
            filtered_bids_data.append(bid_info)
        return filtered_bids_data
        
    @property
    def startDate(self):
        date = iso8601.parse_date(
            self._auction_data['data']['auctionPeriod']['startDate']
        )
        date = date.astimezone(SCHEDULER.timezone)
        if datetime.now(timezone('Europe/Kiev')) > date:
            date = datetime.now(timezone('Europe/Kiev')) + timedelta(seconds=20)
            self._auction_data['data']['auctionPeriod']['startDate'] = date.isoformat()
        return date

    def get_auction_info(self):
        logging.info("Get data from {}".format(self.tender_url))
        response = requests.get(self.tender_url)
        logging.info("Response from {}: {}".format(self.tender_url, response.ok))
        if response.ok:
            self._auction_data = response.json()
        else:
            logging.error("Bad response from {} text {}".format(
                self.tender_url,
                response.text)
            )
            sys.exit(1)
        # self._auction_data = {"data":
        #     {"minimalStep":
        #         {"currency": "UAH", "amount": 35000.0, "valueAddedTaxIncluded": True},
        #      "auctionPeriod": {"startDate": "2014-11-19T11:21:00+00:00", "endDate": None},
        #      "bids": [{"date": "2014-11-19T08:22:21.726234+00:00", "id": "d3ba84c66c9e4f34bfb33cc3c686f137",
        #                "value": {"currency": None, "amount": 475000.0, "valueAddedTaxIncluded": True}},
        #               {"date": "2014-11-19T08:22:24.038426+00:00", "id": "5675acc9232942e8940a034994ad883e",
        #                "value": {"currency": None, "amount": 480000.0, "valueAddedTaxIncluded": True}}],
        #      "tenderID": "UA-9146e92e23c64627bfbbfcdc3ef72eef", "dateModified": "2014-11-19T08:22:24.866669+00:00"}
        #     }
        self.bidders_count = len(self._auction_data["data"]["bids"])
        self.rounds_stages = []
        for stage in range((self.bidders_count + 1) * ROUNDS + 1):
            if (stage + self.bidders_count) % (self.bidders_count + 1) == 0:
                self.rounds_stages.append(stage)
        self.bidders = [bid["id"] for bid in self._auction_data["data"]["bids"]]
        self.mapping = {}
        for index, uid in enumerate(self.bidders):
            self.mapping[uid] = str(index + 1)

    def schedule_auction(self):
        self.get_auction_info()
        # Schedule Auction Workflow
        self.get_auction_document()
        if self.auction_document:
            self.db.delete(self.auction_document)
        auction_document = {"_id": self.auction_doc_id, "stages": [],
                            "tenderID": self._auction_data["data"].get("tenderID", ""),
                            "initial_bids": [], "current_stage": -1,
                            "minimalStep": self._auction_data["data"]["minimalStep"]}
        # Initital Bids
        for bid_info in self._auction_data["data"]["bids"]:

            auction_document["initial_bids"].append(json.loads(INITIAL_BIDS_TEMPLATE.render(
                time="",
                bidder_id=bid_info["id"],
                bidder_name=self.mapping[bid_info["id"]],
                amount="null"
            )))

        SCHEDULER.add_job(self.start_auction, 'date', run_date=self.startDate)
        # Schedule Bids Rounds
        next_stage_timedelta = self.startDate
        for round_id in xrange(ROUNDS):
            # Schedule PAUSE Stage
            pause_stage = json.loads(PAUSE_TEMPLATE.render(
                start=next_stage_timedelta.isoformat()
            ))
            auction_document['stages'].append(pause_stage)
            if round_id == 0:
                next_stage_timedelta += timedelta(seconds=FIRST_PAUSE_SECONDS)
                SCHEDULER.add_job(
                    self.end_first_pause, 'date',
                    run_date=next_stage_timedelta,
                )
            else:
                next_stage_timedelta += timedelta(seconds=PAUSE_SECONDS)
                SCHEDULER.add_job(
                    self.next_stage, 'date',
                    run_date=next_stage_timedelta,
                )

            # Schedule BIDS Stages
            for index in xrange(self.bidders_count):
                bid_stage = json.loads(BIDS_TEMPLATE.render(
                    start=next_stage_timedelta.isoformat(),
                    bidder_id="",
                    bidder_name="",
                    amount="null",
                    time=""
                ))
                auction_document['stages'].append(bid_stage)
                next_stage_timedelta += timedelta(seconds=BIDS_SECONDS)
                SCHEDULER.add_job(
                    self.end_bids_stage, 'date',
                    run_date=next_stage_timedelta,
                )

        announcement = json.loads(ANNOUNCEMENT_TEMPLATE.render(
            start=next_stage_timedelta.isoformat()
        ))
        auction_document['stages'].append(announcement)
        auction_document['endDate'] = next_stage_timedelta.isoformat()
        self.db.save(auction_document)
        self.server = run_server(self)
        SCHEDULER.add_job(
            self.end_auction, 'date',
            run_date=next_stage_timedelta + timedelta(seconds=5)
        )

    def wait_to_end(self):
        self._end_auction_event.wait()
    
    def start_auction(self):
        logging.info('---------------- Start auction ----------------')
        self.get_auction_info()
        self.get_auction_document()
        # Initital Bids
        bids = deepcopy(self._auction_data['data']['bids'])
        self.auction_document["initial_bids"] = []
        bids_info = sorting_start_bids_by_amount(bids)
        for index, bid in enumerate(bids_info):
            self.auction_document["initial_bids"].append(json.loads(INITIAL_BIDS_TEMPLATE.render(
                time=bid["date"] if "date" in bid else self.startDate,
                bidder_id=bid["id"],
                bidder_name=self.mapping[bid["id"]],
                amount=bid["value"]["amount"]
            )))
        self.auction_document["current_stage"] = 0
        all_bids = deepcopy(self.auction_document["initial_bids"])
        minimal_bids = []
        for bidder in self.bidders:
            minimal_bids.append(get_latest_bid_for_bidder(all_bids, str(bidder)))
        minimal_bids = self.filter_bids_keys(sorting_by_amount(minimal_bids))
        self.update_future_bidding_orders(minimal_bids)
        self.save_auction_document()

    def end_first_pause(self):
        logging.info('---------------- End First Pause ----------------')
        self.bids_actions.acquire()
        self.get_auction_document()
        self.auction_document["current_stage"] += 1
        self.save_auction_document()
        self.bids_actions.release()

    def end_bids_stage(self):
        self.bids_actions.acquire()
        self.get_auction_document()
        logging.info('---------------- End Bids Stage ----------------')
        if self.approve_bids_information():
            current_round = self.get_round_number(self.auction_document["current_stage"])
            start_stage, end_stage = self.get_round_stages(current_round)
            all_bids = deepcopy(self.auction_document["stages"][start_stage:end_stage])
            minimal_bids = []
            for bidder_id in self.bidders:
                minimal_bids.append(get_latest_bid_for_bidder(all_bids, bidder_id))
            minimal_bids = self.filter_bids_keys(sorting_by_amount(minimal_bids))
            self.update_future_bidding_orders(minimal_bids)
        self.auction_document["current_stage"] += 1
        logging.info('---------------- Start stage {0} ----------------'.format(
            self.auction_document["current_stage"])
        )
        self.save_auction_document()
        self.bids_actions.release()

    def next_stage(self):
        self.bids_actions.acquire()
        doc = self.db.get(self.auction_doc_id)
        doc["current_stage"] += 1
        self.db.save(doc)
        logging.info('---------------- Start stage {0} ----------------'.format(
            doc["current_stage"])
        )
        self.bids_actions.release()

    def end_auction(self):
        logging.info('---------------- End auction ----------------')
        self.server.stop()
        self.put_auction_data()
        self._end_auction_event.set()

    def approve_bids_information(self):
        current_stage = self.auction_document["current_stage"]
        all_bids = []
        if current_stage in self._bids_data:
            logging.debug("Current stage bids {}".format(self._bids_data[current_stage]))
            all_bids += self._bids_data[current_stage]
        if all_bids:
            bid_info = get_latest_bid_for_bidder(
                all_bids,
                self.auction_document["stages"][current_stage]['bidder_id']
            )

            bid_info = {key: bid_info[key] for key in BIDS_KEYS_FOR_COPY}
            bid_info["bidder_name"] = self.mapping[bid_info['bidder_id']]
            self.auction_document["stages"][current_stage] = generate_bids_stage(
                self.auction_document["stages"][current_stage],
                bid_info
            )
            self.auction_document["stages"][current_stage]["changed"] = True
            return True
        else:
            return False

    def update_future_bidding_orders(self, bids):
        current_round = self.get_round_number(self.auction_document["current_stage"])
        for round_number in range(current_round + 1, ROUNDS + 1):
            for index, stage in enumerate(range(*self.get_round_stages(round_number))):
                self.auction_document["stages"][stage] = generate_bids_stage(
                    self.auction_document["stages"][stage],
                    bids[index]
                )

    def put_auction_data(self):
        self.get_auction_document()
        start_stage, end_stage = self.get_round_stages(ROUNDS)
        all_bids = deepcopy(self.auction_document["stages"][start_stage:end_stage])
        logging.info("Approved data: {}".format(all_bids))
        for index, bid_info in enumerate(self._auction_data["data"]["bids"]):
            auction_bid_info = get_latest_bid_for_bidder(all_bids, bid_info["id"])
            self._auction_data["data"]["bids"][index]["value"]["amount"] = auction_bid_info["amount"]
            self._auction_data["data"]["bids"][index]["date"] = auction_bid_info["time"]
        self._auction_data["data"]["auctionPeriod"]["endDate"] = self.auction_document['endDate']
        self._auction_data["data"]["status"] = "qualification"
        response = requests.patch(
            self.tender_url,
            headers={'content-type': 'application/json'},
            data=json.dumps(self._auction_data)
        )
        if response.ok:
            logging.info('Auction data submitted')
        else:
            logging.warn('Error while submit auction data: {}'.format(response.text))


def auction_run(auction_doc_id, port, database_url, auction_data={}):
    auction = Auction(auction_doc_id, port=port, database_url=database_url,
                      auction_data=auction_data)
    SCHEDULER.start()
    auction.schedule_auction()
    auction.wait_to_end()
    SCHEDULER.shutdown()


def main():
    parser = argparse.ArgumentParser(description='---- Auction ----')
    parser.add_argument('auction_doc_id', type=str, help='auction_doc_id')
    parser.add_argument('port', type=int, help='Port')
    parser.add_argument('database_url', type=str, help='Database Url')
    parser.add_argument('--auction_info', type=str, help='Auction File')
    args = parser.parse_args()
    if args.auction_info:
        auction_data = json.load(open(args.auction_info))
    else:
        auction_data = None
    auction_run(args.auction_doc_id, args.port, args.database_url, auction_data)


##############################################################
if __name__ == "__main__":
    main()

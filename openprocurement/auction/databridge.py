from gevent import monkey
monkey.patch_all()
from openprocurement_client.sync import get_resource_items

try:
    import urllib3.contrib.pyopenssl
    urllib3.contrib.pyopenssl.inject_into_urllib3()
except ImportError:
    pass

import logging
import logging.config
import os
import argparse
from urlparse import urljoin
from zope.interface import implementer
from yaml import load
from couchdb import Database, Session
from dateutil.tz import tzlocal

from openprocurement.auction.interfaces import\
    IAuctionDatabridge, IAuctionsManager
from openprocurement.auction.core import components
from openprocurement.auction.utils import FeedItem

from openprocurement.auction.systemd_msgs_ids import\
    DATA_BRIDGE_PLANNING_DATA_SYNC, DATA_BRIDGE_PLANNING_START_BRIDGE
from openprocurement.auction.design import sync_design


LOGGER = logging.getLogger(__name__)
API_EXTRA = {'opt_fields': 'status,auctionPeriod,lots,procurementMethodType', 'mode': '_all_'}


@implementer(IAuctionDatabridge)
class AuctionsDataBridge(object):

    """Auctions Data Bridge"""

    def __init__(self,
                 config,
                 re_planning=False,
                 debug=False,
                 activate=False):
        super(AuctionsDataBridge, self).__init__()
        self.config = config
        self.resource_ids_list = []
        self.activate = activate
        self.tz = tzlocal()
        self.debug = debug
        self.mapper = components.qA(self, IAuctionsManager)
        self.re_planning = re_planning

        self.couch_url = urljoin(
            self.config_get('couch_url'),
            self.config_get('auctions_db')
        )
        self.db = Database(self.couch_url,
                           session=Session(retry_delays=range(10)))
        sync_design(self.db)

    def config_get(self, name):
        return self.config.get('main').get(name)

    def run(self):
        if self.re_planning:
            self.run_re_planning()
            return

        LOGGER.info('Start Auctions Bridge',
                    extra={'MESSAGE_ID': DATA_BRIDGE_PLANNING_START_BRIDGE})
        LOGGER.info('Start data sync...',
                    extra={'MESSAGE_ID': DATA_BRIDGE_PLANNING_DATA_SYNC})
        for item in get_tenders(host=self.config_get('tenders_api_server'),
                                version=self.config_get('tenders_api_version'),
                                key='', extra_params=API_EXTRA):
            # magic goes here
            feed = FeedItem(item)
            planning = self.mapper(feed)
            if not planning:
                continue
            for cmd, item_id, lot_id in planning:
                if lot_id:
                    LOGGER.info('Lot {} of tender {} selected for {}'.format(lot_id, item_id, cmd))
                else:
                    LOGGER.info('Tender {} selected for {}'.format(item_id, cmd))
                planning(cmd, item_id, lot_id=lot_id)


    def run_re_planning(self):
        pass
        # self.re_planning = True
        # self.offset = ''
        # LOGGER.info('Start Auctions Bridge for re-planning...',
        #             extra={'MESSAGE_ID': DATA_BRIDGE_RE_PLANNING_START_BRIDGE})
        # for tender_item in self.get_teders_list(re_planning=True):
        #     LOGGER.debug('Tender {} selected for re-planning'.format(tender_item))
        #     for planning_data in self.get_teders_list():
        #         if len(planning_data) == 1:
        #             LOGGER.info('Tender {0} selected for planning'.format(*planning_data))
        #             self.start_auction_worker_cmd('planning', planning_data[0])
        #         elif len(planning_data) == 2:
        #             LOGGER.info('Lot {1} of tender {0} selected for planning'.format(*planning_data))
        #             self.start_auction_worker_cmd('planning', planning_data[0], lot_id=planning_data[1])
        #         self.tenders_ids_list.append(tender_item['id'])
        #     sleep(1)
        # LOGGER.info("Re-planning auctions finished",
        #             extra={'MESSAGE_ID': DATA_BRIDGE_RE_PLANNING_FINISHED})


def main():
    parser = argparse.ArgumentParser(description='---- Auctions Bridge ----')
    parser.add_argument('config', type=str, help='Path to configuration file')
    parser.add_argument(
        '--re-planning', action='store_true', default=False,
        help='Not ignore auctions which already scheduled')
    params = parser.parse_args()
    if os.path.isfile(params.config):
        with open(params.config) as config_file_obj:
            config = load(config_file_obj.read())
        logging.config.dictConfig(config)
        bridge = AuctionsDataBridge(config, re_planning=params.re_planning)
        bridge.run()


if __name__ == "__main__":
    main()

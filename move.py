#This file is part of the nodux_account_purchase_liquidation module for Tryton.
#The COPYRIGHT file at the top level of this repository contains
#the full copyright notices and license terms.
from decimal import Decimal
from trytond.model import fields
from trytond.pool import Pool, PoolMeta

__all__ = ['Move']
__metaclass__ = PoolMeta


class Move:
    __name__ = 'account.move'

    @classmethod
    def _get_origin(cls):
        return super(Move, cls)._get_origin() + ['account.liquidation']

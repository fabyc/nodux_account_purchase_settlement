#This file is part of Tryton.  The COPYRIGHT file at the top level of
#this repository contains the full copyright notices and license terms.
from trytond.pool import Pool
from .account import *
from .liquidation import *
from .move import *

def register():
    Pool.register(
        FiscalYear,
        Period,
        Move,
        AccountLiquidation,
        AccountLiquidationTax,
        module='nodux_account_purchase_settlement', type_='model')

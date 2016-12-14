# -*- coding: utf-8 -*-
#This file is part of the nodux_account_purchase_settlement module for Tryton.
#The COPYRIGHT file at the top level of this repository contains
#the full copyright notices and license terms.
from decimal import Decimal
from trytond.model import ModelSingleton, ModelView, ModelSQL, fields, Workflow
from trytond.transaction import Transaction
from trytond.pyson import Eval, In, If, Bool, Id
from trytond.pool import Pool
from trytond.report import Report
import pytz
from datetime import datetime,timedelta
import time
from sql import Literal
from trytond.wizard import Wizard, StateView, StateTransition, StateAction, \
    Button
from trytond import backend
from trytond.tools import reduce_ids, grouped_slice
from sql.conditionals import Coalesce, Case
from sql.aggregate import Count, Sum
from sql.functions import Abs, Sign
from trytond.modules.company import CompanyReport

__all__ = ['AccountLiquidation', 'AccountLiquidationTax']


_STATES = {
    'readonly': Eval('state') != 'draft',
}
_DEPENDS = ['state']

_TYPE = [
    ('out_liquidation', 'Purchase Liquidation'),
]

_TYPE2JOURNAL = {
    'out_liquidation': 'expense',
}

_ZERO = Decimal('0.0')

class AccountLiquidation(ModelSQL, ModelView):
    'Account Liquidation'
    __name__ = 'account.liquidation'
    _rec_name = 'number'

    company = fields.Many2One('company.company', 'Company', required=True,
        states=_STATES, select=True, domain=[
            ('id', If(Eval('context', {}).contains('company'), '=', '!='),
                Eval('context', {}).get('company', -1)),
            ],
        depends=_DEPENDS)
    type = fields.Selection(_TYPE, 'Type', select=True,
        required=True, states={
            'readonly': ((Eval('state') != 'draft')),
            }, depends=['state'])
    number = fields.Char('Number', size=None, select=True)

    reference = fields.Char('Reference', size=None, states=_STATES,
        depends=_DEPENDS)
    description = fields.Char('Description', size=None, states=_STATES,
        depends=_DEPENDS)

    state = fields.Selection([
        ('draft', 'Draft'),
        ('validated', 'Confirm'),
        ('posted', 'Posted'),
        ], 'State', readonly=True)

    liquidation_date = fields.Date('Liquidation Date',
        states={
            'readonly': Eval('state').in_(['posted', 'cancel']),
            'required': Eval('state').in_(['posted']),
            },
        depends=['state'])
    accounting_date = fields.Date('Accounting Date', states=_STATES,
        depends=_DEPENDS)
    party = fields.Many2One('party.party', 'Party',
        required=True, states=_STATES, depends=_DEPENDS)
    party_lang = fields.Function(fields.Char('Party Language'),
        'on_change_with_party_lang')
    liquidation_address = fields.Many2One('party.address', 'Liquidation Address',
        required=True, states=_STATES, depends=['state', 'party'],
        domain=[('party', '=', Eval('party'))])
    currency = fields.Many2One('currency.currency', 'Currency', required=True,
        states={
            'readonly': ((Eval('state') != 'draft')),
            }, depends=['state'])
    currency_digits = fields.Function(fields.Integer('Currency Digits'),
        'on_change_with_currency_digits')
    currency_date = fields.Function(fields.Date('Currency Date'),
        'on_change_with_currency_date')
    journal = fields.Many2One('account.journal', 'Journal', required=True,
        states=_STATES, depends=_DEPENDS)
    move = fields.Many2One('account.move', 'Move', readonly=True)
    account = fields.Many2One('account.account', 'Account', required=True,
        states=_STATES, depends=_DEPENDS)
    taxes = fields.One2Many('account.liquidation.tax', 'liquidation', 'Tax Lines',
        states=_STATES, depends=_DEPENDS)
    comment = fields.Text('Comment', states=_STATES, depends=_DEPENDS)

    untaxed_amount = fields.Function(fields.Numeric('Untaxed',
            digits=(16, Eval('currency_digits', 2)),
            depends=['currency_digits']),
        'get_amount', searcher='search_untaxed_amount')
    tax_amount = fields.Function(fields.Numeric('Tax', digits=(16,
                Eval('currency_digits', 2)), depends=['currency_digits']),
        'get_amount', searcher='search_tax_amount')
    total_amount = fields.Function(fields.Numeric('Total liquidation', digits=(16,
                Eval('currency_digits', 2)), depends=['currency_digits']),
        'get_amount', searcher='search_total_amount')


    @classmethod
    def __setup__(cls):
        super(AccountLiquidation, cls).__setup__()

        cls._error_messages.update({
            'delete_liquidation': 'You can not delete a liquidation that is posted!',
            'no_liquidation_sequence': ('There is no liquidation sequence for '
                'liquidation "%(liquidation)s" on the period/fiscal year '
                '"%(period)s".'),
        })

        cls._buttons.update({
                'validate_liquidation': {
                    'invisible': Eval('state') != 'draft',
                    },

                'post': {
                    'invisible': (Eval('state') == 'posted'),
                    'readonly' : ~Eval('taxes', [0]),
                    #'readonly': Not(Bool(Eval('taxes'))
                    },
                })
        cls._order.insert(0, ('liquidation_date', 'DESC'))

    @staticmethod
    def default_state():
        return 'draft'

    @fields.depends('currency')
    def on_change_with_currency_digits(self, name=None):
        if self.currency:
            return self.currency.digits
        return 2

    @fields.depends('party')
    def on_change_with_party_lang(self, name=None):
        Config = Pool().get('ir.configuration')
        if self.party:
            if self.party.lang:
                return self.party.lang.code
        return Config.get_language()

    def get_tax_context(self):
        context = {}
        if self.party and self.party.lang:
            context['language'] = self.party.lang.code
        return context

    @classmethod
    def get_amount(cls, invoices, names):
        pool = Pool()
        InvoiceTax = pool.get('account.liquidation.tax')
        Move = pool.get('account.move')
        MoveLine = pool.get('account.move.line')
        cursor = Transaction().cursor

        untaxed_amount = dict((i.id, _ZERO) for i in invoices)
        tax_amount = dict((i.id, _ZERO) for i in invoices)
        total_amount = dict((i.id, _ZERO) for i in invoices)

        type_name = cls.tax_amount._field.sql_type().base
        tax = InvoiceTax.__table__()
        to_round = False
        for sub_ids in grouped_slice(invoices):
            red_sql = reduce_ids(tax.liquidation, sub_ids)
            cursor.execute(*tax.select(tax.liquidation,
                    Coalesce(Sum(tax.amount), 0).as_(type_name),
                    where=red_sql,
                    group_by=tax.liquidation))
            for invoice_id, sum_ in cursor.fetchall():
                # SQLite uses float for SUM
                if not isinstance(sum_, Decimal):
                    sum_ = Decimal(str(sum_))
                    to_round = True
                tax_amount[invoice_id] = sum_
        # Float amount must be rounded to get the right precision
        if to_round:
            for invoice in invoices:
                tax_amount[invoice.id] = invoice.currency.round(
                    tax_amount[invoice.id])

        invoices_move = set()
        invoices_no_move = set()
        for invoice in invoices:
            if invoice.move:
                invoices_move.add(invoice.id)
            else:
                invoices_no_move.add(invoice.id)
        invoices_move = cls.browse(invoices_move)
        invoices_no_move = cls.browse(invoices_no_move)

        type_name = cls.total_amount._field.sql_type().base
        invoice = cls.__table__()
        move = Move.__table__()
        line = MoveLine.__table__()
        to_round = False
        for sub_ids in grouped_slice(invoices_move):
            red_sql = reduce_ids(invoice.id, sub_ids)
            cursor.execute(*invoice.join(move,
                    condition=invoice.move == move.id
                    ).join(line, condition=move.id == line.move
                    ).select(invoice.id,
                    Coalesce(Sum(
                            Case((line.second_currency == invoice.currency,
                                    Abs(line.amount_second_currency)
                                    * Sign(line.debit - line.credit)),
                                else_=line.debit - line.credit)),
                        0).cast(type_name),
                    where=(invoice.account == line.account) & red_sql,
                    group_by=invoice.id))
            for invoice_id, sum_ in cursor.fetchall():
                # SQLite uses float for SUM
                if not isinstance(sum_, Decimal):
                    sum_ = Decimal(str(sum_))
                    to_round = True
                total_amount[invoice_id] = sum_

        for invoice in invoices_move:
            # Float amount must be rounded to get the right precision
            if to_round:
                total_amount[invoice.id] = invoice.currency.round(
                    total_amount[invoice.id])
            untaxed_amount[invoice.id] = (
                total_amount[invoice.id] - tax_amount[invoice.id])

        for invoice in invoices_no_move:
            total_amount[invoice.id] = (
                untaxed_amount[invoice.id] + tax_amount[invoice.id])

        result = {
            'untaxed_amount': untaxed_amount,
            'tax_amount': tax_amount,
            'total_amount': total_amount,
            }

        for key in result.keys():
            if key not in names:
                del result[key]
        return result

    @staticmethod
    def default_currency():
        Company = Pool().get('company.company')
        company_id = Transaction().context.get('company')
        if company_id:
            return Company(company_id).currency.id

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @staticmethod
    def default_journal():
        pool = Pool()
        Journal = pool.get('account.journal')
        journal = Journal.search([('type','=', 'expense')])

        for j in journal:
            return j.id

    @staticmethod
    def default_date():
        Date = Pool().get('ir.date')
        return Date.today()

    def set_number(self):
        pool = Pool()
        Period = pool.get('account.period')
        Sequence = pool.get('ir.sequence.strict')
        Date = pool.get('ir.date')

        if self.number:
            return

        test_state = True

        accounting_date = self.accounting_date or self.liquidation_date
        period_id = Period.find(self.company.id,
            date=accounting_date, test_state=test_state)
        period = Period(period_id)
        sequence = period.get_invoice_sequence(self.type)
        if not sequence:
            self.raise_user_error('no_liquidation_sequence', {
                    'liquidation': self.rec_name,
                    'period': period.rec_name,
                    })
        with Transaction().set_context(
                date=self.liquidation_date or Date.today()):
            number = Sequence.get_id(sequence.id)
            vals = {'number': number}
            if (not self.liquidation_date
                    and self.type in ('out_liquidation')):
                vals['liquidation_date'] = Transaction().context['date']
        self.write([self], vals)

    @classmethod
    def delete(cls, liquidations):
        if not liquidations:
            return True
        for liquidation in liquidations:
            if liquidation.state == 'posted':
                cls.raise_user_error('delete_liquidation')
        return super(AccountLiquidation, cls).delete(liquidations)

    def prepare_liquidation_lines(self):
        pool = Pool()
        Period = pool.get('account.period')
        Move = pool.get('account.move')
        Liquidation = pool.get('account.liquidation')
        amount = Decimal(0.0)
        move_lines = []
        line_move_ids = []
        move, = Move.create([{
            'period': Period.find(self.company.id, date=self.liquidation_date),
            'journal': self.journal.id,
            'date': self.liquidation_date,
            'origin': str(self),
        }])

        self.write([self], {
                'move': move.id,
                })

        for tax in self.taxes:
            amount += tax.amount

        if self.type == 'out_liquidation':
            debit = Decimal('0.00')
            credit = amount
        else:
            debit = self.total_amount
            credit = Decimal('0.00')
        move_lines.append({
            'description': self.number,
            'debit': debit,
            'credit': credit,
            'account': self.account.id,
            'move': move.id,
            'journal': self.journal.id,
            'period': Period.find(self.company.id, date=self.liquidation_date),
            })
        if self.taxes:
            for tax in self.taxes:
                if self.type == 'out_liquidation':
                    debit = tax.amount
                    credit = Decimal('0.00')

                move_lines.append({
                    'description': tax.description,
                    'debit': debit,
                    'credit': credit,
                    'account': tax.account.id,
                    'move': move.id,
                    'journal': self.journal.id,
                    'party':self.party,
                    'period': Period.find(self.company.id, date=self.liquidation_date),
                    })
        return move_lines

    def posted(self, move_lines):
        pool = Pool()
        Move = pool.get('account.move')
        MoveLine = pool.get('account.move.line')
        created_lines = MoveLine.create(move_lines)
        Move.post([self.move])
        return True

    @classmethod
    @ModelView.button
    @Workflow.transition('validated')
    def validate_invoice(cls, liquidations):
        for liquidation in liquidations:
            invoice.create_move()

    @classmethod
    @ModelView.button
    def post(cls, liquidations):
        for liquidation in liquidations:
            liquidation.set_number()
            move_lines = liquidation.prepare_liquidation_lines()
            liquidation.posted(move_lines)
        cls.write(liquidations, {'state': 'posted'})

class AccountLiquidationTax(ModelSQL, ModelView):
    'Account Liquidation Tax'
    __name__ = 'account.liquidation.tax'
    _rec_name = 'description'
    liquidation = fields.Many2One('account.liquidation', 'liquidation', ondelete='CASCADE',
            select=True)
    description = fields.Char('Description', size=None, required=True)
    sequence = fields.Integer('Sequence')
    sequence_number = fields.Function(fields.Integer('Sequence Number'),
            'get_sequence_number')
    account = fields.Many2One('account.account', 'Account', required=True,
        domain=[
            ('kind', '!=', 'view'),
            ('company', '=', Eval('_parent_liquidation', {}).get('company', 0)),
            ])
    base = fields.Numeric('Base', required=True,
        digits=(16, Eval('_parent_liquidation', {}).get('currency_digits', 2)))
    amount = fields.Numeric('Amount', required=True,
        digits=(16, Eval('_parent_liquidation', {}).get('currency_digits', 2)),
        depends=['tax', 'base', 'manual'])
    manual = fields.Boolean('Manual')
    base_code = fields.Many2One('account.tax.code', 'Base Code',
        domain=[
            ('company', '=', Eval('_parent_liquidation', {}).get('company', 0)),
            ])
    base_sign = fields.Numeric('Base Sign', digits=(2, 0), required=True)
    tax_code = fields.Many2One('account.tax.code', 'Tax Code',
        domain=[
            ('company', '=', Eval('_parent_liquidation', {}).get('company', 0)),
            ])
    tax_sign = fields.Numeric('Tax Sign', digits=(2, 0), required=True)
    tax = fields.Many2One('account.tax', 'Tax',
        states={
            'readonly': ~Eval('manual', False),
            },
        depends=['manual'])
    tipo = fields.Char('Tipo de retencion')
    @classmethod
    def __setup__(cls):
        super(AccountLiquidationTax, cls).__setup__()
        cls._order.insert(0, ('sequence', 'ASC'))
        cls._error_messages.update({
                'modify': ('You can not modify tax "%(tax)s" from liquidation '
                    '"%(liquidation)s" because it is posted or paid.'),
                'create': ('You can not add line "%(line)s" to liquidation '
                    '"%(liquidation)s" because it is posted, paid or canceled.'),
                'invalid_account_company': ('You can not create liquidation '
                    '"%(liquidation)s" on company "%(liquidation_company)s" using '
                    'account "%(account)s" from company '
                    '"%(account_company)s".'),
                'invalid_base_code_company': ('You can not create liquidation '
                    '"%(liquidation)s" on company "%(liquidation_company)s" '
                    'using base tax code "%(base_code)s" from company '
                    '"%(base_code_company)s".'),
                'invalid_tax_code_company': ('You can not create liquidation '
                    '"%(liquidation)s" on company "%(liquidation_company)s" using tax '
                    'code "%(tax_code)s" from company '
                    '"%(tax_code_company)s".'),
                })

    @classmethod
    def __register__(cls, module_name):
        TableHandler = backend.get('TableHandler')
        cursor = Transaction().cursor
        table = TableHandler(cursor, cls, module_name)

        super(AccountLiquidationTax, cls).__register__(module_name)

        # Migration from 2.4: drop required on sequence
        table.not_null_action('sequence', action='remove')

    @staticmethod
    def order_sequence(tables):
        table, _ = tables[None]
        return [table.sequence == None, table.sequence]

    @staticmethod
    def default_base():
        return Decimal('0.0')

    @staticmethod
    def default_amount():
        return Decimal('0.0')

    @staticmethod
    def default_manual():
        return True

    @staticmethod
    def default_base_sign():
        return Decimal('1')

    @staticmethod
    def default_tax_sign():
        return Decimal('1')

    @fields.depends('tax', '_parent_liquidation.party', '_parent_liquidation.type')
    def on_change_tax(self):
        Tax = Pool().get('account.tax')
        changes = {}
        if self.tax:
            if self.liquidation:
                context = self.liquidation.get_tax_context()
            else:
                context = {}
            with Transaction().set_context(**context):
                tax = Tax(self.tax.id)
            changes['description'] = tax.description
            if self.liquidation and self.liquidation.type:
                liquidation_type = self.liquidation.type
            else:
                liquidation_type = 'out_liquidation'
            if liquidation_type in ('out_liquidation', 'in_liquidation'):
                changes['base_code'] = (tax.invoice_base_code.id
                    if tax.invoice_base_code else None)
                changes['base_sign'] = tax.invoice_base_sign
                changes['tax_code'] = (tax.invoice_tax_code.id
                    if tax.invoice_tax_code else None)
                changes['tax_sign'] = tax.invoice_tax_sign
                changes['account'] = tax.invoice_account.id
        return changes

    @fields.depends('tax', 'base', 'amount', 'manual')
    def on_change_with_amount(self):
        Tax = Pool().get('account.tax')
        transaction = Transaction()
        company = transaction.context['company']
        Company = Pool().get('company.company')
        companies = Company.search([('id', '=', company)])
        for c in companies:
            company = c
        if self.tax and self.manual:

            tax = self.tax
            base = self.base or Decimal(0)
            for values in Tax.compute([tax], base, 1):
                if (values['tax'] == tax
                        and values['base'] == base):
                    amount = company.currency.round(values['amount'])
                    return amount
        return company.currency.round(self.amount)

    @classmethod
    def check_modify(cls, taxes):
        '''
        Check if the taxes can be modified
        '''
        for tax in taxes:
            if tax.liquidation.state in ('posted', 'paid'):
                cls.raise_user_error('modify')

    def get_sequence_number(self, name):
        i = 1
        for tax in self.liquidation.taxes:
            if tax == self:
                return i
            i += 1
        return 0

    @classmethod
    def delete(cls, taxes):
        cls.check_modify(taxes)
        super(AccountLiquidationTax, cls).delete(taxes)

    @classmethod
    def write(cls, *args):
        taxes = sum(args[0::2], [])
        cls.check_modify(taxes)
        super(AccountLiquidationTax, cls).write(*args)

    @classmethod
    def create(cls, vlist):
        Liquidation = Pool().get('account.liquidation')
        liquidation_ids = []
        for vals in vlist:
            if vals.get('Liquidation'):
                liquidation_ids.append(vals['liquidation'])
        for liquidation in Liquidation.browse(liquidation_ids):
            if liquidation.state in ('posted'):
                cls.raise_user_error('create')
        return super(AccountLiquidationTax, cls).create(vlist)

    @classmethod
    def validate(cls, taxes):
        super(AccountLiquidationTax, cls).validate(taxes)
        for tax in taxes:
            tax.check_company()

    def check_company(self):
        company = self.liquidation.company
        if self.account.company != company:
            self.raise_user_error('invalid_account_company', {
                    'liquidation': self.liquidation.rec_name,
                    'liquidation_company': self.liquidation.company.rec_name,
                    'account': self.account.rec_name,
                    'account_company': self.account.company.rec_name,
                    })
        if self.base_code:
            if self.base_code.company != company:
                self.raise_user_error('invalid_base_code_company', {
                        'liquidation': self.liquidation.rec_name,
                        'liquidation_company': self.liquidation.company.rec_name,
                        'base_code': self.base_code.rec_name,
                        'base_code_company': self.base_code.company.rec_name,
                        })
        if self.tax_code:
            if self.tax_code.company != company:
                self.raise_user_error('invalid_tax_code_company', {
                        'liquidation': self.liquidation.rec_name,
                        'liquidation_company': self.liquidation.company.rec_name,
                        'tax_code': self.tax_code.rec_name,
                        'tax_code_company': self.tax_code.company.rec_name,
                        })

    def get_move_line(self):
        '''
        Return a list of move lines values for liquidation tax
        '''
        Currency = Pool().get('currency.currency')
        res = {}
        if not self.amount:
            return []
        res['description'] = self.description
        if self.liquidation.currency != self.liquidation.company.currency:
            with Transaction().set_context(date=self.liquidation.currency_date):
                amount = Currency.compute(self.liquidation.currency, self.amount,
                    self.liquidation.company.currency)
            res['amount_second_currency'] = self.amount
            res['second_currency'] = self.liquidation.currency.id
        else:
            amount = self.amount
            res['amount_second_currency'] = None
            res['second_currency'] = None
        if self.liquidation.type in ('in_liquidation', 'out_credit_note'):
            if amount >= Decimal('0.0'):
                res['debit'] = amount
                res['credit'] = Decimal('0.0')
            else:
                res['debit'] = Decimal('0.0')
                res['credit'] = - amount
                if res['amount_second_currency']:
                    res['amount_second_currency'] = \
                        - res['amount_second_currency']
        else:
            if amount >= Decimal('0.0'):
                res['debit'] = Decimal('0.0')
                res['credit'] = amount
                if res['amount_second_currency']:
                    res['amount_second_currency'] = \
                        - res['amount_second_currency']
            else:
                res['debit'] = - amount
                res['credit'] = Decimal('0.0')
        res['account'] = self.account.id
        if self.account.party_required:
            res['party'] = self.liquidation.party.id
        if self.tax_code:
            res['tax_lines'] = [('create', [{
                            'code': self.tax_code.id,
                            'amount': amount * self.tax_sign,
                            'tax': self.tax and self.tax.id or None
                            }])]
        return [res]

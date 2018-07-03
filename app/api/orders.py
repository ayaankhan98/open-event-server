from datetime import datetime

from flask import render_template
from flask_jwt import current_identity as current_user
from flask_rest_jsonapi import ResourceDetail, ResourceList, ResourceRelationship
from marshmallow_jsonapi import fields
from marshmallow_jsonapi.flask import Schema
from sqlalchemy.orm.exc import NoResultFound

from app.api.data_layers.ChargesLayer import ChargesLayer
from app.api.helpers.db import save_to_db, safe_query, safe_query_without_soft_deleted_entries
from app.api.helpers.exceptions import ForbiddenException, UnprocessableEntity, ConflictException
from app.api.helpers.files import create_save_pdf
from app.api.helpers.files import make_frontend_url
from app.api.helpers.mail import send_email_to_attendees
from app.api.helpers.mail import send_order_cancel_email
from app.api.helpers.notification import send_notif_to_attendees, send_notif_ticket_purchase_organizer, \
    send_notif_ticket_cancel
from app.api.helpers.permission_manager import has_access
from app.api.helpers.permissions import jwt_required
from app.api.helpers.query import event_query
from app.api.helpers.ticketing import TicketingManager
from app.api.helpers.utilities import dasherize, require_relationship
from app.api.schema.orders import OrderSchema
from app.models import db
from app.models.discount_code import DiscountCode, TICKET
from app.models.order import Order, OrderTicket
from app.models.ticket_holder import TicketHolder


class OrdersListPost(ResourceList):
    """
    OrderListPost class for OrderSchema
    """

    def before_post(self, args, kwargs, data=None):
        """
        before post method to check for required relationships and permissions
        :param args:
        :param kwargs:
        :param data:
        :return:
        """
        require_relationship(['event', 'ticket_holders'], data)
        # Ensuring that default status is always pending, unless the user is event co-organizer
        if not has_access('is_coorganizer', event_id=data['event']):
            data['status'] = 'pending'

    def before_create_object(self, data, view_kwargs):
        """
        before create object method for OrderListPost Class
        :param data:
        :param view_kwargs:
        :return:
        """
        for ticket_holder in data['ticket_holders']:
            # Ensuring that the attendee exists and doesn't have an associated order.
            try:
                ticket_holder_object = self.session.query(TicketHolder).filter_by(id=int(ticket_holder),
                                                                                  deleted_at=None).one()
                if ticket_holder_object.order_id:
                    raise ConflictException({'pointer': '/data/relationships/attendees'},
                                            "Order already exists for attendee with id {}".format(str(ticket_holder)))
            except NoResultFound:
                raise ConflictException({'pointer': '/data/relationships/attendees'},
                                        "Attendee with id {} does not exists".format(str(ticket_holder)))

        if data.get('cancel_note'):
            del data['cancel_note']

        # Apply discount only if the user is not event admin
        if data.get('discount') and not has_access('is_coorganizer', event_id=data['event']):
            discount_code = safe_query_without_soft_deleted_entries(self, DiscountCode, 'id', data['discount'],
                                                                    'discount_code_id')
            if not discount_code.is_active:
                raise UnprocessableEntity({'source': 'discount_code_id'}, "Inactive Discount Code")
            else:
                now = datetime.utcnow()
                valid_from = datetime.strptime(discount_code.valid_from, '%Y-%m-%d %H:%M:%S')
                valid_till = datetime.strptime(discount_code.valid_till, '%Y-%m-%d %H:%M:%S')
                if not (valid_from <= now <= valid_till):
                    raise UnprocessableEntity({'source': 'discount_code_id'}, "Inactive Discount Code")
                if not TicketingManager.match_discount_quantity(discount_code, data['ticket_holders']):
                    raise UnprocessableEntity({'source': 'discount_code_id'}, 'Discount Usage Exceeded')

            if discount_code.event.id != data['event'] and discount_code.user_for == TICKET:
                raise UnprocessableEntity({'source': 'discount_code_id'}, "Invalid Discount Code")

    def after_create_object(self, order, data, view_kwargs):
        """
        after create object method for OrderListPost Class
        :param order:
        :param data:
        :param view_kwargs:
        :return:
        """
        order_tickets = {}
        for holder in order.ticket_holders:
            if holder.id != current_user.id:
                pdf = create_save_pdf(render_template('pdf/ticket_attendee.html', order=order, holder=holder),
                                      dir_path='/static/uploads/pdf/tickets/')
            else:
                pdf = create_save_pdf(render_template('pdf/ticket_purchaser.html', order=order),
                                      dir_path='/static/uploads/pdf/tickets/')
            holder.pdf_url = pdf
            save_to_db(holder)
            if not order_tickets.get(holder.ticket_id):
                order_tickets[holder.ticket_id] = 1
            else:
                order_tickets[holder.ticket_id] += 1
        for ticket in order_tickets:
            od = OrderTicket(order_id=order.id, ticket_id=ticket, quantity=order_tickets[ticket])
            save_to_db(od)
        order.quantity = order.get_tickets_count()
        order.user = current_user
        save_to_db(order)
        if not has_access('is_coorganizer', event_id=data['event']):
            TicketingManager.calculate_update_amount(order)

        # send e-mail and notifications if the order status is completed
        if order.status == 'completed':
            send_email_to_attendees(order, current_user.id)
            send_notif_to_attendees(order, current_user.id)

            order_url = make_frontend_url(path='/orders/{identifier}'.format(identifier=order.identifier))
            for organizer in order.event.organizers:
                send_notif_ticket_purchase_organizer(organizer, order.invoice_number, order_url, order.event.name)

        data['user_id'] = current_user.id

    methods = ['POST', ]
    decorators = (jwt_required,)
    schema = OrderSchema
    data_layer = {'session': db.session,
                  'model': Order,
                  'methods': {'before_create_object': before_create_object,
                              'after_create_object': after_create_object
                              }}


class OrdersList(ResourceList):
    """
    OrderList class for OrderSchema
    """

    def before_get(self, args, kwargs):
        """
        before get method to get the resource id for fetching details
        :param args:
        :param kwargs:
        :return:
        """
        if kwargs.get('event_id') and not has_access('is_coorganizer', event_id=kwargs['event_id']):
            raise ForbiddenException({'source': ''}, "Co-Organizer Access Required")

    def query(self, view_kwargs):
        query_ = self.session.query(Order)
        query_ = event_query(self, query_, view_kwargs)

        return query_

    decorators = (jwt_required,)
    methods = ['GET', ]
    schema = OrderSchema
    data_layer = {'session': db.session,
                  'model': Order,
                  'methods': {
                      'query': query
                  }}


class OrderDetail(ResourceDetail):
    """
    OrderDetail class for OrderSchema
    """

    def before_get_object(self, view_kwargs):
        """
        before get method to get the resource id for fetching details
        :param view_kwargs:
        :return:
        """
        if view_kwargs.get('attendee_id'):
            attendee = safe_query(self, TicketHolder, 'id', view_kwargs['attendee_id'], 'attendee_id')
            view_kwargs['order_identifier'] = attendee.order.identifier

        order = safe_query(self, Order, 'identifier', view_kwargs['order_identifier'], 'order_identifier')

        if not has_access('is_coorganizer_or_user_itself', event_id=order.event_id, user_id=order.user_id):
            return ForbiddenException({'source': ''}, 'You can only access your orders or your event\'s orders')

    def before_update_object(self, order, data, view_kwargs):
        """
        :param order:
        :param data:
        :param view_kwargs:
        :return:
        """
        # Admin can update all the fields while Co-organizer can update only the status
        if not has_access('is_admin'):
            for element in data:
                if element != 'status':
                    setattr(data, element, getattr(order, element))

        if not has_access('is_coorganizer', event_id=order.event.id):
            raise ForbiddenException({'pointer': 'data/status'},
                                     "To update status minimum Co-organizer access required")

        if 'order_notes' in data:
            if order.order_notes and data['order_notes'] not in order.order_notes.split(","):
                data['order_notes'] = '{},{}'.format(order.order_notes, data['order_notes'])

    def after_update_object(self, order, data, view_kwargs):
        """
        :param order:
        :param data:
        :param view_kwargs:
        :return:
        """
        if order.status == 'cancelled':
            send_order_cancel_email(order)
            send_notif_ticket_cancel(order)

    def before_delete_object(self, order, view_kwargs):
        """
        method to check for proper permissions for deleting
        :param order:
        :param view_kwargs:
        :return:
        """
        if not has_access('is_coorganizer', event_id=order.event.id):
            raise ForbiddenException({'source': ''}, 'Access Forbidden')

    decorators = (jwt_required,)

    schema = OrderSchema
    data_layer = {'session': db.session,
                  'model': Order,
                  'url_field': 'order_identifier',
                  'id_field': 'identifier',
                  'methods': {
                      'before_update_object': before_update_object,
                      'before_delete_object': before_delete_object,
                      'before_get_object': before_get_object,
                      'after_update_object': after_update_object
                  }}


class OrderRelationship(ResourceRelationship):
    """
    Order relationship
    """
    decorators = (jwt_required,)
    schema = OrderSchema
    data_layer = {'session': db.session,
                  'model': Order}


class ChargeSchema(Schema):
    """
    ChargeSchema
    """

    class Meta:
        """
        Meta class for ChargeSchema
        """
        type_ = 'charge'
        inflect = dasherize
        self_view = 'v1.charge_list'
        self_view_kwargs = {'id': '<id>'}

    id = fields.Str(dump_only=True)
    stripe = fields.Str(allow_none=True)
    paypal = fields.Str(allow_none=True)


class ChargeList(ResourceList):
    """
    ChargeList ResourceList for ChargesLayer class
    """
    methods = ['POST', ]
    schema = ChargeSchema

    data_layer = {
        'class': ChargesLayer,
        'session': db.session
    }

    decorators = (jwt_required,)
# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, exceptions, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_round
from odoo.addons import decimal_precision as dp


class StockMoveLine(models.Model):
    _inherit = 'stock.move.line'

    workorder_id = fields.Many2one('mrp.workorder', 'Work Order')
    production_id = fields.Many2one('mrp.production', 'Production Order')
    lot_produced_id = fields.Many2one('stock.production.lot', 'Finished Lot')
    lot_produced_qty = fields.Float('Quantity Finished Product', help="Informative, not used in matching")
    done_wo = fields.Boolean('Done for Work Order', default=True, help="Technical Field which is False when temporarily filled in in work order")  # TDE FIXME: naming
    done_move = fields.Boolean('Move Done', related='move_id.is_done', store=True)  # TDE FIXME: naming

    def _get_similar_move_lines(self):
        lines = super(StockMoveLine, self)._get_similar_move_lines()
        if self.move_id.production_id:
            finished_moves = self.move_id.production_id.move_finished_ids
            finished_move_lines = finished_moves.mapped('move_line_ids')
            lines |= finished_move_lines.filtered(lambda ml: ml.product_id == self.product_id and (ml.lot_id or ml.lot_name))
        if self.move_id.raw_material_production_id:
            raw_moves = self.move_id.raw_material_production_id.move_raw_ids
            raw_moves_lines = raw_moves.mapped('move_line_ids')
            raw_moves_lines |= self.move_id.active_move_line_ids
            lines |= raw_moves_lines.filtered(lambda ml: ml.product_id == self.product_id and (ml.lot_id or ml.lot_name))
        return lines

    @api.multi
    def write(self, vals):
        for move_line in self:
            if move_line.production_id and 'lot_id' in vals:
                move_line.production_id.move_raw_ids.mapped('move_line_ids')\
                    .filtered(lambda r: r.done_wo and not r.done_move and r.lot_produced_id == move_line.lot_id)\
                    .write({'lot_produced_id': vals['lot_id']})
            production = move_line.move_id.production_id or move_line.move_id.raw_material_production_id
            if production and move_line.state == 'done' and any(field in vals for field in ('lot_id', 'location_id', 'qty_done')):
                move_line._log_message(production, move_line, 'mrp.track_production_move_template', vals)
        return super(StockMoveLine, self).write(vals)


class StockMove(models.Model):
    _inherit = 'stock.move'

    created_production_id = fields.Many2one('mrp.production', 'Created Production Order')
    production_id = fields.Many2one(
        'mrp.production', 'Production Order for finished products')
    raw_material_production_id = fields.Many2one(
        'mrp.production', 'Production Order for raw materials')
    unbuild_id = fields.Many2one(
        'mrp.unbuild', 'Disassembly Order')
    consume_unbuild_id = fields.Many2one(
        'mrp.unbuild', 'Consumed Disassembly Order')
    operation_id = fields.Many2one(
        'mrp.routing.workcenter', 'Operation To Consume')  # TDE FIXME: naming
    workorder_id = fields.Many2one(
        'mrp.workorder', 'Work Order To Consume')
    # Quantities to process, in normalized UoMs
    active_move_line_ids = fields.One2many('stock.move.line', 'move_id', domain=[('done_wo', '=', True)], string='Lots')
    bom_line_id = fields.Many2one('mrp.bom.line', 'BoM Line')
    unit_factor = fields.Float('Unit Factor')
    is_done = fields.Boolean(
        'Done', compute='_compute_is_done',
        store=True,
        help='Technical Field to order moves')
    needs_lots = fields.Boolean('Tracking', compute='_compute_needs_lots')
    order_finished_lot_ids = fields.Many2many('stock.production.lot', compute='_compute_order_finished_lot_ids')
    finished_lots_exist = fields.Boolean('Finished Lots Exist', compute='_compute_order_finished_lot_ids')

    @api.depends('active_move_line_ids.qty_done', 'active_move_line_ids.product_uom_id')
    def _compute_done_quantity(self):
        super(StockMove, self)._compute_done_quantity()

    @api.depends('raw_material_production_id.move_finished_ids.move_line_ids.lot_id')
    def _compute_order_finished_lot_ids(self):
        for move in self:
            if move.raw_material_production_id.move_finished_ids:
                finished_lots_ids = move.raw_material_production_id.move_finished_ids.mapped('move_line_ids.lot_id').ids
                if finished_lots_ids:
                    move.order_finished_lot_ids = finished_lots_ids
                    move.finished_lots_exist = True
                else:
                    move.finished_lots_exist = False

    @api.depends('product_id.tracking')
    def _compute_needs_lots(self):
        for move in self:
            move.needs_lots = move.product_id.tracking != 'none'

    @api.depends('raw_material_production_id.is_locked', 'picking_id.is_locked')
    def _compute_is_locked(self):
        super(StockMove, self)._compute_is_locked()
        for move in self:
            if move.raw_material_production_id:
                move.is_locked = move.raw_material_production_id.is_locked

    def _get_move_lines(self):
        self.ensure_one()
        if self.raw_material_production_id:
            return self.active_move_line_ids
        else:
            return super(StockMove, self)._get_move_lines()

    @api.depends('state')
    def _compute_is_done(self):
        for move in self:
            move.is_done = (move.state in ('done', 'cancel'))

    @api.model
    def default_get(self, fields_list):
        defaults = super(StockMove, self).default_get(fields_list)
        if self.env.context.get('default_raw_material_production_id'):
            production_id = self.env['mrp.production'].browse(self.env.context['default_raw_material_production_id'])
            if production_id.state == 'done':
                defaults['state'] = 'done'
                defaults['product_uom_qty'] = 0.0
                defaults['additional'] = True
        return defaults

    def _action_assign(self):
        res = super(StockMove, self)._action_assign()
        for move in self.filtered(lambda x: x.production_id or x.raw_material_production_id):
            if move.move_line_ids:
                move.move_line_ids.write({'production_id': move.raw_material_production_id.id,
                                               'workorder_id': move.workorder_id.id,})
        return res

    def _action_cancel(self):
        if any(move.quantity_done and (move.raw_material_production_id or move.production_id) for move in self):
            raise exceptions.UserError(_('You cannot cancel a manufacturing order if you have already consumed material.\
             If you want to cancel this MO, please change the consumed quantities to 0.'))
        return super(StockMove, self)._action_cancel()

    def _action_confirm(self, merge=True):
        moves = self.env['stock.move']
        for move in self:
            moves |= move.action_explode()
        # we go further with the list of ids potentially changed by action_explode
        return super(StockMove, moves)._action_confirm(merge=merge)

    def action_explode(self):
        """ Explodes pickings """
        # in order to explode a move, we must have a picking_type_id on that move because otherwise the move
        # won't be assigned to a picking and it would be weird to explode a move into several if they aren't
        # all grouped in the same picking.
        if not self.picking_type_id:
            return self
        bom = self.env['mrp.bom'].sudo()._bom_find(product=self.product_id)
        if not bom or bom.type != 'phantom':
            return self
        phantom_moves = self.env['stock.move']
        processed_moves = self.env['stock.move']
        factor = self.product_uom._compute_quantity(self.product_uom_qty, bom.product_uom_id) / bom.product_qty
        boms, lines = bom.sudo().explode(self.product_id, factor, picking_type=bom.picking_type_id)
        for bom_line, line_data in lines:
            phantom_moves += self._generate_move_phantom(bom_line, line_data['qty'])

        for new_move in phantom_moves:
            processed_moves |= new_move.action_explode()
#         if not self.split_from and self.procurement_id:
#             # Check if procurements have been made to wait for
#             moves = self.procurement_id.move_ids
#             if len(moves) == 1:
#                 self.procurement_id.write({'state': 'done'})
        if processed_moves and self.state == 'assigned':
            # Set the state of resulting moves according to 'assigned' as the original move is assigned
            processed_moves.write({'state': 'assigned'})
        # delete the move with original product which is not relevant anymore
        self.sudo().unlink()
        return processed_moves

    def _prepare_phantom_move_values(self, bom_line, quantity):
        return {
            'picking_id': self.picking_id.id if self.picking_id else False,
            'product_id': bom_line.product_id.id,
            'product_uom': bom_line.product_uom_id.id,
            'product_uom_qty': quantity,
            'state': 'draft',  # will be confirmed below
            'name': self.name,
        }

    def _generate_move_phantom(self, bom_line, quantity):
        if bom_line.product_id.type in ['product', 'consu']:
            return self.copy(default=self._prepare_phantom_move_values(bom_line, quantity))
        return self.env['stock.move']

    def _generate_consumed_move_line(self, qty_to_add, final_lot, lot=False):
        if lot:
            ml = self.move_line_ids.filtered(lambda ml: ml.lot_id == lot and not ml.lot_produced_id)
        else:
            ml = self.move_line_ids.filtered(lambda ml: not ml.lot_id and not ml.lot_produced_id)
        if ml:
            new_quantity_done = (ml.qty_done + qty_to_add)
            if new_quantity_done >= ml.product_uom_qty:
                ml.write({'qty_done': new_quantity_done, 'lot_produced_id': final_lot.id})
            else:
                new_qty_reserved = ml.product_uom_qty - new_quantity_done
                default = {'product_uom_qty': new_quantity_done,
                           'qty_done': new_quantity_done,
                           'lot_produced_id': final_lot.id}
                ml.copy(default=default)
                ml.with_context(bypass_reservation_update=True).write({'product_uom_qty': new_qty_reserved, 'qty_done': 0})
        else:
            vals = {
                'move_id': self.id,
                'product_id': self.product_id.id,
                'location_id': self.location_id.id,
                'location_dest_id': self.location_dest_id.id,
                'product_uom_qty': 0,
                'product_uom_id': self.product_uom.id,
                'qty_done': qty_to_add,
                'lot_produced_id': final_lot.id,
            }
            if lot:
                vals.update({'lot_id': lot.id})
            self.env['stock.move.line'].create(vals)


class PushedFlow(models.Model):
    _inherit = "stock.location.path"

    def _prepare_move_copy_values(self, move_to_copy, new_date):
        new_move_vals = super(PushedFlow, self)._prepare_move_copy_values(move_to_copy, new_date)
        new_move_vals['production_id'] = False

        return new_move_vals

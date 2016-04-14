# -*- coding:utf-8 -*-
import json
import logging

from django.db.models import Q

from networkapi.ambiente.models import Ambiente, EnvironmentVip
from networkapi.api_equipment.exceptions import AllEquipmentsAreInMaintenanceException
from networkapi.api_equipment.facade import all_equipments_are_in_maintenance
from networkapi.api_pools.facade import get_pool_by_ids
from networkapi.api_pools.serializers import PoolV3Serializer
from networkapi.api_vip_request import exceptions, models
from networkapi.distributedlock import distributedlock, LOCK_VIP
from networkapi.equipamento.models import Equipamento, EquipamentoAcesso, EquipamentoAmbiente
from networkapi.infrastructure.datatable import build_query_to_datatable
from networkapi.ip.models import Ip, Ipv6
from networkapi.plugins.factory import PluginFactory
from networkapi.requisicaovips.models import OptionVip, ServerPoolMember
from networkapi.util import valid_expression


protocolo_access = 'https'
log = logging.getLogger(__name__)


def get_vip_request(vip_request_ids):
    """
    get Vip Request
    """
    vip_requests = models.VipRequest.objects.filter(id__in=vip_request_ids)

    return vip_requests


def create_vip_request(vip_request):
    """
    Create Vip Request
    """
    validate_save(vip_request)

    vip = models.VipRequest()
    vip.name = vip_request['name']
    vip.service = vip_request['service']
    vip.business = vip_request['business']
    vip.environmentvip_id = vip_request['environmentvip']
    vip.ipv4 = Ip.get_by_pk(vip_request['ipv4']['id']) if vip_request['ipv4'] else None
    vip.ipv6 = Ipv6.get_by_pk(vip_request['ipv6']['id']) if vip_request['ipv6'] else None

    option_create = [vip_request['options'][key] for key in vip_request['options']]
    vip.save()

    _create_port(vip_request['ports'], vip.id)
    _create_option(option_create, vip.id)

    return vip


def update_vip_request(vip_request):
    """
    update Vip Request
    """
    validate_save(vip_request)

    vip = models.VipRequest.objects.get(
        id=vip_request.get('id')
    )
    vip.name = vip_request['name']
    vip.service = vip_request['service']
    vip.business = vip_request['business']
    vip.environmentvip_id = vip_request['environmentvip']
    vip.ipv4 = Ip.get_by_pk(vip_request['ipv4']['id']) if vip_request['ipv4'] else None
    vip.ipv6 = Ipv6.get_by_pk(vip_request['ipv6']['id']) if vip_request['ipv6'] else None

    option_ids = [option.id for option in vip.viprequestoptionvip_set.all()]
    options = [vip_request['options'][key] for key in vip_request['options']]
    option_remove = list(set(option_ids) - set(options))
    option_create = list(set(options) - set(option_ids))

    vip.save()

    _update_port(vip_request['ports'], vip.id)

    _create_option(option_create, vip.id)
    _delete_option(option_remove)

    dsrl3 = OptionVip.objects.filter(nome_opcao_txt='DSRL3', tipo_opcao='Retorno de trafego').values('id')
    if dsrl3:
        if dsrl3[0]['id'] in option_remove:
            models.VipRequestDSCP.objects.filter(vip_request=vip.id).delete()


def delete_vip_request(vip_request_ids):
    """delete vip request"""

    models.VipRequest.objects.filter(id__in=vip_request_ids).delete()


def _create_port(ports, vip_request_id):
    """Create ports"""

    for port in ports:
        # save port
        pt = models.VipRequestPort()
        pt.vip_request_id = vip_request_id
        pt.port = port['port']
        pt.save()

        # save port option l7_protocol
        opt = models.VipRequestPortOptionVip()
        opt.vip_request_port_id = pt.id
        opt.optionvip_id = port['options']['l4_protocol']
        opt.save()

        # save port option l7_protocol
        opt = models.VipRequestPortOptionVip()
        opt.vip_request_port_id = pt.id
        opt.optionvip_id = port['options']['l7_protocol']
        opt.save()

        # save pool by port
        for pool in port.get('pools'):
            pl = models.VipRequestPortPool()
            pl.vip_request_port_id = pt.id
            pl.server_pool_id = pool['server_pool']
            pl.optionvip_id = pool['l7_rule']
            pl.val_optionvip = pool['l7_value']
            pl.save()


def _update_port(ports, vip_request_id):
    """Update ports"""
    for port in ports:
        # save port
        try:
            pt = models.VipRequestPort.objects.get(
                vip_request_id=vip_request_id,
                port=port['port'])
        except:
            pt = models.VipRequestPort()
            pt.vip_request_id = vip_request_id
            pt.port = port['port']
            pt.save()

        # save port option l4_protocol
        try:
            opt = models.VipRequestPortOptionVip.objects.get(
                vip_request_port_id=pt.id,
                optionvip_id=port['options']['l4_protocol'])
        except:
            opt = models.VipRequestPortOptionVip()
            opt.vip_request_port_id = pt.id
            opt.optionvip_id = port['options']['l4_protocol']
            opt.save()

        # save port option l7_protocol
        try:
            opt = models.VipRequestPortOptionVip.objects.get(
                vip_request_port_id=pt.id,
                optionvip_id=port['options']['l7_protocol'])
        except:
            opt = models.VipRequestPortOptionVip()
            opt.vip_request_port_id = pt.id
            opt.optionvip_id = port['options']['l7_protocol']
            opt.save()

        # save pool by port
        for pool in port.get('pools'):
            try:
                pl = models.VipRequestPortPool.objects.get(
                    vip_request_port=pt.id,
                    server_pool_id=pool['server_pool'])
            except:
                pl = models.VipRequestPortPool()
                pl.vip_request_port_id = pt.id
                pl.server_pool_id = pool['server_pool']
            finally:
                if pl.optionvip_id != pool['l7_rule'] or pl.val_optionvip != pool['l7_value']:
                    pl.optionvip_id = pool['l7_rule']
                    pl.val_optionvip = pool['l7_value']
                    pl.save()

        # delete pool by port
        pools = [pool['server_pool'] for pool in port.get('pools')]
        models.VipRequestPortPool.objects.filter(
            vip_request_port=pt
        ).exclude(
            server_pool__in=pools
        ).delete()

    # delete port
    ports_ids = [port['port'] for port in ports]
    models.VipRequestPort.objects.filter(
        vip_request_id=vip_request_id
    ).exclude(
        port__in=ports_ids
    ).delete()


def _create_option(options, vip_request_id):
    """create options"""

    for option in options:
        opt = models.VipRequestOptionVip()
        opt.vip_request_id = vip_request_id
        opt.optionvip_id = option
        opt.save()

    dsrl3 = OptionVip.objects.filter(nome_opcao_txt='DSRL3', tipo_opcao='Retorno de trafego').values('id')
    if dsrl3:
        if dsrl3[0]['id'] in options:
            dscp = _dscp(vip_request_id)
            vip_dscp = models.VipRequestDSCP()
            vip_dscp.vip_request = vip_request_id
            vip_dscp.dscp = dscp


def _delete_option(options):
    """Deletes options"""
    models.VipRequestOptionVip.objects.filter(id__in=options).delete()


def get_vip_request_by_search(search=dict()):

    vip_requests = models.VipRequest.objects.filter()

    if search.get('asorting_cols'):
        search['asorting_cols'] = search.get('asorting_cols').split(';')

    vip_requests, total = build_query_to_datatable(
        vip_requests,
        search.get('asorting_cols') or None,
        search.get('custom_search') or None,
        search.get('searchable_columns') or None,
        search.get('start_record') or 0,
        search.get('end_record') or 25)

    vip_map = dict()
    vip_map["vips"] = vip_requests
    vip_map["total"] = total

    return vip_map


def create_real_vip_request(vip_requests):

    load_balance = dict()

    for vip_request in vip_requests:

        equips, conf, cluster_unit = _validate_vip_to_apply(vip_request)

        for idx, pool in enumerate(vip_request['pools']):
            pool = get_pool_by_ids([pool['server_pool']])
            pool_serializer = PoolV3Serializer(pool[0])
            vip_request['pools'][idx]['server_pool'] = pool_serializer.data
        vip_request['conf'] = conf

        if conf:
            conf = json.loads(conf)
            for layer in conf['conf']['layers']:
                requiments = layer.get('requiments')
                if requiments:
                    for requiment in requiments:
                        condicionals = requiment.get('condicionals')
                        for condicional in condicionals:

                            validated = True

                            validations = condicional.get('validations')
                            for validation in validations:
                                if validation.get('type') == 'optionvip':
                                    vip_request['optionsvip'][validation.get('variable')]
                                    validated &= valid_expression(
                                        validation.get('operator'),
                                        vip_request['optionsvip'][validation.get('variable')],
                                        validation.get('value')
                                    )

                                if validation.get('type') == 'field' and validation.get('variable') == 'cluster_unit':
                                    validated &= valid_expression(
                                        validation.get('operator'),
                                        cluster_unit,
                                        validation.get('value')
                                    )

                            if validated:
                                use = condicional.get('use')
                                for item in use:
                                    definitions = item.get('definitions')
                                    eqpts = item.get('eqpts')
                                    for eqpt in eqpts:
                                        eqpt_id = str(eqpt)

                                        if not load_balance.get(eqpt_id):
                                            equipment_access = EquipamentoAcesso.search(
                                                equipamento=eqpt,
                                                protocolo=protocolo_access
                                            ).uniqueResult()
                                            equipment = Equipamento.get_by_pk(eqpt)

                                            plugin = PluginFactory.factory(equipment)

                                            load_balance[eqpt_id] = {
                                                'plugin': plugin,
                                                'fqdn': equipment_access.fqdn,
                                                'user': equipment_access.user,
                                                'password': equipment_access.password,
                                                'vips': [],
                                            }

                                        load_balance[eqpt_id]['vips'].append({
                                            'definitions': definitions
                                        })

        for e in equips:
            eqpt_id = str(e.equipamento.id)

            if not load_balance.get(eqpt_id):
                equipment_access = EquipamentoAcesso.search(
                    equipamento=e.equipamento.id,
                    protocolo=protocolo_access
                ).uniqueResult()
                equipment = Equipamento.get_by_pk(e.equipamento.id)

                plugin = PluginFactory.factory(equipment)

                load_balance[eqpt_id] = {
                    'plugin': plugin,
                    'fqdn': equipment_access.fqdn,
                    'user': equipment_access.user,
                    'password': equipment_access.password,
                    'vips': [],
                }

            load_balance[eqpt_id]['vips'].append({'vip_request': vip_request})

    for lb in load_balance:
        load_balance[lb]['plugin'].create_vip(load_balance[lb])

    ids = [vip_request.get('id') for vip_request in vip_requests]
    models.VipRequest.objects.filter(id__in=ids).update(created=True)


#############
# helpers
#############
def create_lock(vip_requests):
    """
    Create locks to vips list
    """
    locks_list = list()
    for vip_request in vip_requests:
        lock = distributedlock(LOCK_VIP % vip_request['id'])
        lock.__enter__()
        locks_list.append(lock)

    return locks_list


def destroy_lock(locks_list):
    """
    Destroy locks by vips list
    """
    for lock in locks_list:
        lock.__exit__('', '', '')


def validate_save(vip_request, permit_created=False):

    has_identifier = models.VipRequest.objects.filter(
        environmentvip__environmentenvironmentvip__environment__in=Ambiente.objects.filter(
            environmentenvironmentvip__environment_vip=vip_request['environmentvip'])
    )

    # validate ipv4
    if vip_request['ipv4']:
        has_identifier = has_identifier.filter(ipv4=vip_request['ipv4']['id'])

        vips = EnvironmentVip.objects.filter(
            environmentenvironmentvip__environment__vlan__networkipv4__ip=vip_request['ipv4']['id']
        ).filter(
            id=vip_request['environmentvip']
        )
        if not vips:
            raise exceptions.IpNotFoundByEnvironment()

    # validate ipv6
    if vip_request['ipv6']:
        has_identifier = has_identifier.filter(ipv6=vip_request['ipv6']['id'])
        vips = EnvironmentVip.objects.filter(
            environmentenvironmentvip__environment__vlan__networkipv6__ipv6=vip_request['ipv6']['id']
        ).filter(
            id=vip_request['environmentvip']
        )
        if not vips:
            raise exceptions.IpNotFoundByEnvironment()

    # validate change info when vip created
    if vip_request.get('id'):
        vip = models.VipRequest.objects.get(id=vip_request.get('id'))
        if vip.created:
            if not permit_created:
                raise exceptions.CreatedVipRequestValuesException('vip request id: %s' % vip_request.get('id'))

            if vip.name != vip_request['name'] or vip.environmentvip_id != vip_request['environmentvip']:
                raise exceptions.CreatedVipRequestValuesException('vip request id: %s' % vip_request.get('id'))

        has_identifier = has_identifier.exclude(id=vip_request.get('id'))

    has_identifier = has_identifier.distinct()
    if has_identifier.count() > 0:
        raise exceptions.AlreadyVipRequestException()

    opts = list()
    for option in vip_request['options']:
        opt = dict()
        if option == 'cache_group' and vip_request['options'].get(option) is not None:
            opt['id'] = vip_request['options'].get(option)
            opt['tipo_opcao'] = 'cache'
        elif option == 'persistence' and vip_request['options'].get(option) is not None:
            opt['id'] = vip_request['options'].get(option)
            opt['tipo_opcao'] = 'Persistencia'
        elif option == 'traffic_return' and vip_request['options'].get(option) is not None:
            opt['id'] = vip_request['options'].get(option)
            opt['tipo_opcao'] = 'Retorno de trafego'
        elif option == 'timeout' and vip_request['options'].get(option) is not None:
            opt['id'] = vip_request['options'].get(option)
            opt['tipo_opcao'] = 'timeout'
        if opt:
            opts.append(opt)

    options = OptionVip.objects.filter(
        reduce(lambda x, y: x | y, [Q(**op) for op in opts]),
        optionvipenvironmentvip__environment__id=vip_request['environmentvip']
    )

    if len(opts) != options.count():
        raise Exception('Invalid Options to VipRequest %s' % vip_request['name'])

    # validate pools associates
    for port in vip_request.get('ports'):

        opts = list()
        for option in port['options']:
            opt = dict()
            if option == 'l4_protocol' and port['options'].get(option) is not None:
                opt['id'] = port['options'].get(option)
                opt['tipo_opcao'] = 'l4_protocol'
            elif option == 'l7_protocol' and port['options'].get(option) is not None:
                opt['id'] = port['options'].get(option)
                opt['tipo_opcao'] = 'l7_protocol'
            if opt:
                opts.append(opt)

        options = OptionVip.objects.filter(
            reduce(lambda x, y: x | y, [Q(**op) for op in opts]),
            optionvipenvironmentvip__environment__id=vip_request['environmentvip']
        )

        if len(opts) != options.count():
            raise Exception('Invalid Options to port %s of VipRequest %s' % (port['port'], vip_request['name']))

        for pool in port['pools']:

            spms = ServerPoolMember.objects.filter(server_pool=pool['server_pool'])
            for spm in spms:
                if spm.ip:

                    vips = EnvironmentVip.objects.filter(
                        environmentenvironmentvip__environment__vlan__networkipv4__ip=spm.ip
                    ).filter(
                        id=vip_request['environmentvip']
                    )
                    if not vips:
                        raise exceptions.ServerPoolMemberDiffEnvironmentVipException(spm.identifier)
                if spm.ipv6:

                    vips = EnvironmentVip.objects.filter(
                        environmentenvironmentvip__environment__vlan__networkipv6__ipv6=spm.ipv6
                    ).filter(
                        id=vip_request['environmentvip']
                    )
                    if not vips:
                        raise exceptions.ServerPoolMemberDiffEnvironmentVipException(spm.identifier)


def _dscp(vip_request_id):
    members = models.VipRequestDSCP.objects.filter(
        vip_request__viprequestport__viprequestportpool__server_pool__serverpoolmember__in=ServerPoolMember.objects.filter(
            server_pool__viprequestport__viprequestportpool__vip_request__id=vip_request_id)).distinct().values('dscp')
    mb = [i.get('dscp') for i in members]
    perm = range(3, 64)
    perm_new = list(set(perm) - set(mb))
    if perm_new:
        return perm_new[0]
    else:
        raise Exception('Can\'t use pool because pool members have dscp is sold out')


def _validate_vip_to_apply(vip_request, update=False):
    vip = models.VipRequest.objects.get(
        name=vip_request['name'],
        environmentvip=vip_request['environmentvip'],
        id=vip_request.get('id'))
    if not vip:
        raise exceptions.VipRequestDoesNotExistException()

    if update and not vip.created:
        raise exceptions.VipRequestNotCreated(vip.id)

    equips = EquipamentoAmbiente.objects.filter(
        equipamento__maintenance=0,
        ambiente__environmentenvironmentvip__environment_vip__id=vip_request['environmentvip'],
        equipamento__tipo_equipamento__tipo_equipamento=u'Balanceador')

    conf = EnvironmentVip.objects.get(id=vip_request['environmentvip']).conf

    if all_equipments_are_in_maintenance([e.equipamento for e in equips]):
        raise AllEquipmentsAreInMaintenanceException()

    cluster_unit = vip.ipv4.networkipv4.cluster_unit if vip.ipv4 else vip.ipv6.networkipv6.cluster_unit

    return equips, conf, cluster_unit
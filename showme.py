import sys

from helpers import opcode, C, prettify
from contract import load_contract
from storage import read_address

from functools import partial
from collections import defaultdict

from trace import walk_trace, walk_exp

if len(sys.argv) > 1:
    param = sys.argv[1]

    addr_list = {
        'kitties': '0x06012c8cf97BEaD5deAe237070F9587f8E7A266d',
        'default': '0x2Ad180cBAFFbc97237F572148Fc1B283b68D8861',
        'digix': '0xe0b7927c4af23765cb51314a0e0521a9645f0e2a',
        'aragon': '0x960b236a07cf122663c4303350609a66a7b288c0',
        'medianizer': '0x729d19f657bd0614b4985cf1d82531c67569197b',
        'arbitrager': '0xc2a694c5ced27e3d3a5a8bd515a42f2b89665003',
        'nocode': '0x1f772db718238d8413bad9b309950a9c5286fd71',
        'destruct': '0xB02bD126cd5477b2C166f8A31fAb75DB0c074371',
    }

    if param in addr_list:
        contract_name = param
        address = addr_list[param]
    else:
        address = param
        contract_name = None

else:
    print("\n\n\tusage `python showme.py {address}`\n\n")
    exit()


roles = {}


functions, stor_defs = load_contract(address, contract_name)
pretty = partial(prettify, roles)

def add_role(name=None, value=None, definition=None):
    global roles

    if name is None:
        assert definition is not None
        if opcode(definition) == 'STORAGE':
            name = f'stor_{definition[3]}'
        else:
            name = str(definition)

    s = definition or name

    if s in roles:
        return

    roles[s] = {
        'name':name,
        'definition': definition,
        'setters': list(),
        'funcs': set(),
        'withdrawals': set(),
        'calls': set(),
        'value': value,
        'destructs': set(),
        'destructs_init': set(),
    }

for s, name in stor_defs.items():
    if s[:3] == ('STORAGE', 160, 0) and len(s) == 4:
        role_address = read_address(address, s[3])
        add_role(name, role_address, s)


'''

    find storages without getters

'''
def find_storages(exp):
    if opcode(exp) == 'STORAGE':
        return exp

    if opcode(exp) == 'STORE':
        return ('STORAGE', )+exp[1:4]



for f in functions.values():
    trace = f['trace']
    storages = walk_exp(trace, find_storages)
    for s in storages:
        if s not in roles and len(s) == 4 and s[:3] == ('STORAGE', 160, 0):
            add_role(s)

'''
    other roles
'''

add_role('unknown')


def find_opcodes(line, _):
    return opcode(line)

def get_caller_cond(condition):

    if opcode(condition) != 'EQ':
        if opcode(condition) != 'ISZERO':
            return None
        else:
            condition = condition[1]

    if opcode(condition) != 'EQ':
        return None

    if condition[1] == ('MASK_SHL', 160, 0, 0, 'CALLER'):
        stor = condition[2]
    elif condition[2] == ('MASK_SHL', 160, 0, 0, 'CALLER'):
        stor = condition[1]
    else:
        return None

    if opcode(stor) == 'STORAGE' and len(stor) == 4:
        # len(stor) == 5 -> indexed storage array, not handling those now
        return stor
    else:
        return None


def find_caller_req(line, _):
    # finds IFs: (IF (EQ caller, storage))

    if opcode(line) != 'IF':
        return None

    condition, if_true, if_false = line[1:]

    return get_caller_cond(condition)



''' finding a list of admins '''

open_access = set(f['hash'] for f in functions.values())

for f in functions.values():
    trace = f['trace']
    assert type(trace) == tuple

    res = walk_trace(trace, find_caller_req)
    if len(res) > 0:
        f['admins'] = set()
        for r in res:
            f['admins'].add(r)
            if r not in roles:
                add_role(definition=r)
            roles[r]['funcs'].add(f['hash'])
#            if r in roles.keys():
#                roles[r]['funcs'].add(f['hash'])
            if f['hash'] in open_access:
                open_access.remove(f['hash'])

    opcodes = walk_trace(trace, find_opcodes)
    side_effects = ['CALL', 'DELEGATECALL', 'CODECALL', 'SELFDESTRUCT', 'STORE']
    # WARN: ^ the above may not be a complete list

    if all(s not in opcodes for s in side_effects):
        # read_only function
        if f['hash'] in open_access:
            open_access.remove(f['hash'])


''' find who can change a given storage '''

def find_stor_req(line, knows_true):
    if opcode(line) != 'STORE':
        return None

    size, offset, stor_num, arr_idx, value = line[1:]

    if len(arr_idx) > 0:
        # we're dealing only with storages that are not arrays
        return None

    callers = []
    for cond in knows_true:
        caller = get_caller_cond(cond)
        if caller is not None:
            callers.append(caller)

    if len(callers) == 0:
        callers = ['anyone']

    return ('STORAGE', size, offset, stor_num), callers

for f in functions.values():
    trace = f['trace']

    res = walk_trace(trace, find_stor_req)
    if len(res) > 0:
        for (stor, callers) in res:

            affected_roles = set()
            for r in roles:

                if opcode(r) != 'STORAGE':
                    continue

                stor_offset, stor_size, stor_num = stor[2], stor[1], stor[3]
                role_offset, role_size, role_num = r[2], r[1], r[3]

                if stor_offset >= role_offset and stor_offset < role_offset + role_size and stor_num == role_num:
                    affected_roles.add(r)

            # ^ we can't compare roles to storage writes, because that would miss all the partial writes
            # to a given storage. see 'digix' contract, and how setOwner is set there

            setter = (callers, f['name'])

            for role_id in affected_roles:
                if setter not in roles[role_id]['setters']:
                    roles[role_id]['setters'].append(setter)


'''

    browse all the contract calls, and figure out who gets withdrawals, and what contracts
    can get called

'''


def find_calls(line, _):
    # todo: delegatecalls
    # todo: selfdestructs
    if opcode(line) != 'CALL':
        return None

    _, addr, wei, _, _, _, _, f_name, f_params = line[1:]

    if addr == ('MASK_SHL', 160, 0, 0, 'CALLER'):
        # WARN: should check for knows_true, perhaps a caller can only be someone specific
        addr = 'anyone'
    elif opcode(addr) != 'STORAGE' or len(addr) > 4:
        addr = 'unknown'

    return (addr, wei, f_name, f_params)


for f in functions.values():
    trace = f['trace']

    res = walk_trace(trace, find_calls)

    for addr, wei, f_name, f_params in res:
        if addr not in roles:
            add_role(definition=addr)

        if wei != 0:
            # withdrawal
            roles[addr]['withdrawals'].add(f['hash'])
        else:
            roles[addr]['calls'].add(f['hash'])


'''

    find self-destructs

'''

def find_destructs(line, knows_true):
    # todo: delegatecalls
    # todo: selfdestructs
    if opcode(line) != 'SELFDESTRUCT':
        return None

    receiver = line[1]

    if receiver == ('MASK_SHL', 160, 0, 0, 'CALLER'):
        # WARN: should check for knows_true, perhaps a caller can only be someone specific
        receiver = 'anyone'
    elif opcode(receiver) != 'STORAGE' or len(receiver) > 4:
        receiver = 'unknown'

    callers = []
    for cond in knows_true:
        caller = get_caller_cond(cond)
        if caller is not None:
            callers.append(caller)

    if len(callers) == 0:
        callers = ['anyone']

    return receiver, callers


for f in functions.values():
    trace = f['trace']

    res = walk_trace(trace, find_destructs)

    for receiver, callers in res:
        if receiver not in roles:
            add_role(definition=addr)

        roles[receiver]['destructs'].add(f['hash'])

        for caller in callers:
            roles[caller]['destructs_init'].add(f['hash'])



'''

    display

'''
print(f'\n{C.blue} # contract roles{C.end}')
print()

for stor in roles:

    role = roles[stor]

    if len(role['funcs']) == 0 and len(role['withdrawals']) == 0 and len(role['calls']) == 0:
        continue

    print(C.blue, pretty(stor),C.end)

    if roles[stor]['setters']:
        print('  can be changed by:')
        for callers, f_name in roles[stor]['setters']:
            print('  ', C.green, (', '.join(pretty(c) for c in callers)), C.end, 'in', f_name)
        print()
    else:
        if opcode(stor) == 'STORAGE':
            print('  constant')
            print()

    if len(role['funcs']) > 0:
        print('  can call those functions:')

        for f_hash in role['funcs']:
            func = functions[f_hash]

            print('   ', func['color_name'])
        print()

    if len(role['withdrawals']) > 0:
        print('  can receive withdrawal through:')

        for f_hash in role['withdrawals']:
            func = functions[f_hash]

            print('   ', func['color_name'])

        print()

    if len(role['calls']) > 0:
        print('  can be called by:')

        for f_hash in role['calls']:
            func = functions[f_hash]

            print('   ', func['color_name'])

        print()

    if len(role['destructs']) > 0:
        print('  can receive selfdestruct:')

        for f_hash in role['destructs']:
            func = functions[f_hash]

            print('   ', func['color_name'])

        print()

    if len(role['destructs_init']) > 0:
        print('  can initiate selfdestruct:')

        for f_hash in role['destructs_init']:
            func = functions[f_hash]

            print('   ', func['color_name'])

        print()

    print('  current value:\n','  ',str(roles[stor]['value']))



    print()


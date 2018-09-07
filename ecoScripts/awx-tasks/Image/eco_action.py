"""
The purpose of this script is to call other scripts. There are no arguments to 
this script. An environment variable named ACTION defines which action should
be taken (which script to run).
"""

import subprocess
import os
import json
from pigeon import Pigeon

# Define pigeon messenger
pigeon = Pigeon()

if os.getenv('ACTION'):
    pigeon.sendInfoMessage('Starting action ' + os.environ['ACTION'])
    options = {
        'TEST_CONNECTIVITY': lambda : subprocess.call(["python", "test_connectivity.py"]),
        'RUN_INTEGRATION': lambda : subprocess.call(["python", "run-templates.py"]),
        'VALIDATE': lambda : subprocess.call(["python", "validate.py"]),
        'FETCH_ITEMS': lambda : subprocess.call(["python", "fetch-items.py"])
    }
    result = options[os.environ['ACTION']]()
else:
    pigeon.sendUpdate({
        'status': 'unknown',
        'message': 'Action: ' + os.getenv('ACTION') + 'not implemented'
    })

# print a message that the container has completed its work
pigeon.sendInfoMessage('Action complete')

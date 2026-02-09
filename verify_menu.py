import leads
import io
import sys
from unittest.mock import patch

# Mock input to exit immediately if it reached ask()
with patch('utils.ask', return_value='7'):
    with patch('sys.stdout', new=io.StringIO()) as fake_out:
        try:
            leads.menu_leads()
        except:
            pass
        print(fake_out.getvalue())

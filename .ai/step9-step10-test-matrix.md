# Step 9/10 Test Matrix

## Unit
- tests/unit/test_gmail_connector.py
- tests/unit/test_gmail_agent.py
- tests/unit/test_whatsapp_msg_agent.py
- tests/unit/test_manager_agent.py (outbound failure envelope + dry-run mode)
- tests/unit/test_main_startup.py
- tests/unit/test_main_dry_run.py
- tests/unit/test_main_shutdown.py

## Integration
- tests/integration/test_outbound_routing_integration.py
- tests/integration/test_gmail_live.py
- tests/integration/test_main_runtime_integration.py
- tests/integration/test_main_dry_run_integration.py

## Verification Commands
1. python -m poetry run pytest tests/unit/test_gmail_connector.py tests/unit/test_gmail_agent.py tests/unit/test_whatsapp_msg_agent.py tests/unit/test_manager_agent.py -q
2. python -m poetry run pytest tests/integration/test_outbound_routing_integration.py tests/integration/test_gmail_live.py -q
3. python -m poetry run pytest tests/unit/test_main_startup.py tests/unit/test_main_dry_run.py tests/unit/test_main_shutdown.py -q
4. python -m poetry run pytest tests/integration/test_main_runtime_integration.py tests/integration/test_main_dry_run_integration.py -q
5. python -m poetry run python -m src.main --dry-run
6. python -m poetry run pytest -q

# Состояние проверки частичной сводки Ozon

17 ноября 2026 года, рабочая копия Windows. Реализация локальная; реальные
учётные записи, ключи, данные кабинета, API Ozon, клиент Claude, Linux, CI,
развёртывание и опубликованный MCP в этом этапе не проверялись. Страницы
публичной документации Ozon проверены отдельно как источники схем.

## Результат

Добавлены частичные инструменты `ozon_article_summary` и
`ozon_posting_candidates`. Для идентификации сверяются прямой и обратный
ответы Ozon; при неоднозначности финансовый запрос не запускается. Начисления
отбираются по дате начисления и предоставленным номерам отправлений. Данные
группируются по наблюдаемым SKU, видам и валютам без неоснованного
распределения расходов. Число выкупов, возвраты, полный итог и показатели
на единицу остаются неизвестными; список кандидатов из FBO/FBS не доказывает
полноту периода начислений. Подробности и источники —
[OZON_ARTICLE_SUMMARY.md](OZON_ARTICLE_SUMMARY.md).

## Проверки

Рабочая папка команд: `services/email-auth`. Использован Python из
`C:\Users\rebel\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`.
Все проверочные ответы и учётные данные синтетические.

```powershell
& 'C:\Users\rebel\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -B -m unittest -q test_ozon_posting_candidates test_ozon_article_summary test_ozon_posting_status test_finance_gate test_ozon_details test_ozon_mcp test_external_mcp test_external_mcp_integration test_ozon_response_guard test_provider_payload.ProviderPayloadTests test_provider_deadline
```

Результат: **142 теста, OK**. Включены многозначные SKU, изоляция валют,
неполнота/дубли ответов, повтор курсора, ограничение gate при 429 на каждом
из трёх запросов, предел общего времени и синтетический вызов MCP.

Дополнительный прогон использовал принудительную сборку мусора перед удалением
временных SQLite-каталогов Windows:

```powershell
@'
import gc,io,sys,tempfile,unittest
original_cleanup=tempfile.TemporaryDirectory.cleanup
def cleanup_after_gc(self):
    gc.collect()
    return original_cleanup(self)
tempfile.TemporaryDirectory.cleanup=cleanup_after_gc
names=['test_ozon_posting_candidates','test_ozon_article_summary','test_ozon_posting_status','test_finance_gate','test_ozon_details','test_ozon_mcp','test_external_mcp','test_external_mcp_integration','test_ozon_response_guard','test_provider_payload.ProviderPayloadTests','test_provider_deadline','test_mcp_gateway','test_connection_check','test_oauth_mcp','test_oauth_integration','test_bridge_activity','test_request_budget','test_data_preview','test_wb_mcp']
result=unittest.TextTestRunner(stream=io.StringIO(),verbosity=0).run(unittest.defaultTestLoader.loadTestsFromNames(names))
print('tests=',result.testsRun,'failures=',len(result.failures),'errors=',len(result.errors),'skipped=',len(result.skipped))
for kind,items in [('ERROR',result.errors),('FAIL',result.failures)]:
    for test,trace in items: print(kind,test.id(),'Windows file lock' if 'WinError 32' in trace else trace)
sys.exit(0 if result.wasSuccessful() else 1)
'@ | & 'C:\Users\rebel\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -B -
```

Результат: **240 тестов, ошибок 0, пропусков 0**. В этот набор входят
существующие синтетические проверки шлюза MCP, OAuth и Wildberries. Это не
проверка настоящих ответов маркетплейсов или установленных прав.

`git diff --check` завершился успешно; были только предупреждения Git о
переводах строк при следующей записи. `test_release` не запускался в Windows
из-за зависимости от Linux-модуля `fcntl`; фактическая сборка контейнера и
проверка на Linux остаются необходимыми перед развёртыванием.

## Оставшиеся основания для полного отчёта

Нужно подтвердить полный источник начислений за период и пагинацию,
историческую принадлежность SKU, смысл и аддитивность финансовых строк,
распределение многотоварных расходов и источник фактических покупательских
выкупов и возвратов. После этого необходима сверка с настоящим кабинетом и
независимая приёмка результатов. До этих проверок готовым полным финансовым
отчётом считать реализацию нельзя.

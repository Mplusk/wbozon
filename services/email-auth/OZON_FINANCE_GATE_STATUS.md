# Локальный статус Ozon finance gate

Дата: 17 ноября 2026 года. Основа изменений — локальный коммит `9eab7ea`.
Использовались только синтетические ключи, ответы и локальные процессы. Сеть
с реальными данными, production, push, merge и deploy не использовались.

Для операций `ozon_accrual_types` и `ozon_posting_accruals` добавлен локальный
gate: процессы с одним Client ID и общей доверенной директорией не делают
финансовые запросы одновременно. Сервер не делает автоматического повторного
запроса к Ozon. Валидный `Retry-After` сохраняется и передаётся клиенту как
безопасное число секунд, если оно не воспроизводит ключ. HTTP-date принимается
только при наличии корректного `Date` от сервера; используется более длинный
из вариантов по часам клиента и сервера. Неизвестный интервал, совпадение с
ключом или неизвестный результат после прерывания оставляет повтор заблокированным.

Gate не управляет другими приложениями продавца или процессами, использующими
другие каталоги/хосты. Без общей надёжной файловой системы, на которой
поддерживаются проверенные блокировки и атомарная замена, обещание общей
сериализации не действует. Потеря или повреждение реестра, маркера, файлов
клиента или несовпадение поколения останавливают запрос. Одновременный откат
внутреннего состояния и соответствующего внешнего epoch до согласованного
прошлого снимка обнаружить без независимого доверенного anti-rollback источника
нельзя. Указания и границы ручного восстановления — в [PORTAL.md](PORTAL.md).
Функция восстановления не проверяет заявленное внешнее подтверждение времени.

В ответах Ozon одинаковые проекции строк сохраняются без удаления. Сумма
возвращённых строк обозначена как `observed_row_sum`, а не подтверждённый
финансовый итог; `financial_total` остаётся `null`. Идентичность и аддитивность
операций, полнота ответа и совпадение с кабинетом не подтверждены. Номера
запрошенных/полученных отправлений показаны для обнаружения пересекающихся
выборок. Пустой результат не выдаётся за подтверждённый ноль. Большой ответ
MCP не обрезается: возвращается `response_too_large` с подсказкой о `summary`
или уменьшении партии.

Проверка официальных публичных источников: Ozon описывает отдельные методы
[начислений](https://dev.ozon.ru/community/2008-Novye-beta-metody-dlia-polucheniia-nachislenii/)
и предупреждает о возможных [расхождениях с кабинетом](https://dev.ozon.ru/news/783-Izmeneniia-v-metodakh-dlia-raboty-s-finansovymi-otchetami-v-Seller-API/).
Эти источники не подтверждают уникальность строк, правило дедупликации или
аддитивность всех видов начислений. Поэтому полная сводка по артикулу,
получение всей совокупности за период, фактические выкупы и выплата на единицу
остаются заблокированы до подтверждённых источников и проверки кабинета.

## Проверки

Из `services/email-auth` локальным Python 3.12 выполнено:

```powershell
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m unittest -q test_finance_gate test_ozon_details test_ozon_mcp test_external_mcp test_external_mcp_integration test_ozon_response_guard test_provider_payload.ProviderPayloadTests test_provider_deadline
```

Результат: **115 тестов прошли**. Среди них синтетические 429 с числовым и
HTTP-date `Retry-After`, неизвестный/некорректный интервал, повтор до и после
истечения, два процесса, завершение процесса, потеря/подмена файлов, откат
каталога, смена boot identity, совпадение интервала с ключом, ошибки подготовки
до сети и известные ответы 200 с некорректным или слишком большим телом.

Дополнительно **213 тестов прошли** в Windows с диагностической обёрткой,
которая вызывает `gc.collect()` перед `TemporaryDirectory.cleanup`, чтобы
закрыть неосвобождённые SQLite-дескрипторы. Обёртка запускала список модулей:

```text
test_finance_gate test_ozon_details test_ozon_mcp test_external_mcp
test_external_mcp_integration test_ozon_response_guard
test_provider_payload.ProviderPayloadTests test_provider_deadline
test_mcp_gateway test_connection_check test_oauth_mcp test_oauth_integration
test_bridge_activity test_request_budget test_data_preview test_wb_mcp
```

Обёртку можно воспроизвести из того же каталога PowerShell-командой:

```powershell
@'
import gc, io, sys, tempfile, unittest
original_cleanup = tempfile.TemporaryDirectory.cleanup
def cleanup_after_gc(self):
    gc.collect()
    return original_cleanup(self)
tempfile.TemporaryDirectory.cleanup = cleanup_after_gc
names = ['test_finance_gate','test_ozon_details','test_ozon_mcp',
         'test_external_mcp','test_external_mcp_integration',
         'test_ozon_response_guard','test_provider_payload.ProviderPayloadTests',
         'test_provider_deadline','test_mcp_gateway','test_connection_check',
         'test_oauth_mcp','test_oauth_integration','test_bridge_activity',
         'test_request_budget','test_data_preview','test_wb_mcp']
suite = unittest.defaultTestLoader.loadTestsFromNames(names)
result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
print('tests=', result.testsRun, 'failures=', len(result.failures),
      'errors=', len(result.errors), 'skipped=', len(result.skipped))
sys.exit(0 if result.wasSuccessful() else 1)
'@ | & "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -B -
```

Это не обычный прогон CI: полный обычный набор ранее на Windows завершался
ошибками очистки SQLite и импортом Linux-зависимого `fcntl`. Linux, контейнерная
сборка, GitHub Actions, реальный кабинет и Ozon API не проверялись. Проверка
исполнителя не является независимой приёмкой; отдельный отчёт ревизора нужен
для его выводов.

# Project KB Data Safety Policy

Этот документ — нормативный владелец правил безопасного обращения Project KB с source repository, Git, registry, snapshots и локальным managed storage. Обзор компонентов и потоков принадлежит [Project KB Architecture](ARCHITECTURE.md); здесь фиксируются только разрешения, запреты, инварианты и условия отказа.

## 1. Scope

Policy распространяется на:

- регистрацию, relink и unregister проектов;
- чтение source repository и Git metadata;
- canonical capture, publication и structural reads;
- internal lifecycle operations и managed generations;
- status/currentness verification;
- frozen operator-pinned MCP reads;
- все пути внутри `<PROJECT_KB_HOME>`.

Policy не даёт разрешения изменять source repository и не расширяет замороженный capability contract. Generic search, export generation и context-pack generation остаются недоступными.

## 2. Trust and ownership model

Source repository, его файлы, Git metadata и filesystem redirects считаются недоверенным входом. Project KB может извлекать из них структурные факты, но не получает ownership над source tree.

`<PROJECT_KB_HOME>` — единственная managed write boundary продукта. Registry service владеет control-plane state. Canonical `IndexService` владеет текущим per-project snapshot. Internal lifecycle generation service владеет create-once generation files и связанными registry records. Frozen MCP transport не владеет читаемым artifact и не имеет права его изменять.

Доверие к snapshot определяется проверенной identity, а не только именем или расположением файла. Для live canonical lane необходимы active project/workspace binding и snapshot metadata. Для frozen MCP lane необходим полный operator-pinned identity contract.

## 3. Allowed persistent writes

Project KB MUST ограничивать persistent writes следующими managed locations:

| Managed location | Разрешённые записи |
| --- | --- |
| `<PROJECT_KB_HOME>/registry.sqlite` | Создание и transactional update registry v4; принадлежащие SQLite временные transaction sidecars рядом с базой |
| `<PROJECT_KB_HOME>/projects/<project_id>/` | Создание только самого project storage root; строка не разрешает произвольные persistent descendants |
| `<PROJECT_KB_HOME>/projects/<project_id>/kb.sqlite` | Атомарная canonical publication |
| `<PROJECT_KB_HOME>/projects/<project_id>/runs/` | Canonical run records и принадлежащие операции временные snapshot files |
| `<PROJECT_KB_HOME>/projects/<project_id>/exports/` | Только storage container; его наличие не разрешает export generation |
| `<PROJECT_KB_HOME>/generations/<shard>/<snapshot_id>.sqlite` | Create-once managed generation publication |
| Managed generation directory рядом с final file | Только принадлежащий текущей operation временный generation file и необходимые SQLite sidecars до cleanup/publication |
| `<PROJECT_KB_HOME>/scratch/currentness/` | Родитель и очищаемые per-call temporary verification directories |

Создание managed directories разрешено только для явно перечисленных owned locations или как создание необходимых parent directories этих exact locations; это не даёт общего разрешения на произвольные persistent descendants. Внутри project storage durable child writes ограничены `kb.sqlite`, `runs/`, `exports/` container и иными source-verified owned paths, только если они прямо перечислены в этой policy. Любая запись в source repository, его `.git` storage или произвольный внешний путь MUST NOT выполняться.

Resolver/status lookup, предназначенный для чтения существующего state, MUST NOT создавать отсутствующие managed home, registry или project storage. Их создание принадлежит явным mutating operations, таким как `register`.

## 4. Source repository safety

При capture и currentness verification Project KB MUST:

- обращаться с repository как с read-only входом;
- формировать population из Git-tracked и untracked non-ignored paths с ограниченным policy discovery;
- читать содержимое только у допустимого regular text file после path и identity checks;
- не импортировать, не выполнять и не загружать как plugin код целевого проекта;
- сохранять структурные производные данные, а не verbatim source body;
- представлять symlink, junction, reparse point и иной redirect только metadata, не следуя к target content;
- повторно проверять filesystem/source evidence там, где чтение может участвовать в proof стабильности.

Binary data, oversized files, неподдерживаемые extensions и неподдерживаемая encoding остаются metadata-only. Поддерживаемый Python source разбирается стандартным AST parser из текста; target module при этом не импортируется. Ошибка разбора отдельного допустимого файла может быть записана как структурный факт, но не разрешает обход safety checks. Неустойчивая source state не может считаться доказанным стабильным capture.

## 5. Git isolation rules

Git runner MUST принимать только явно allowlisted read-only invocations и MUST отключать pager, hooks, lazy fetch и optional locks там, где это задаёт текущий runner contract. Неожиданные аргументы или subcommands должны отклоняться до запуска Git.

Единственная write-shaped операция Git, используемая для доказательства population, — `update-index --index-info` с отдельным временным `GIT_INDEX_FILE`. Этот index MUST находиться вне source repository, MUST быть очищен владельцем операции и MUST NOT заменять или изменять live Git index.

Capture, currentness и structural query MUST NOT изменять worktree, live Git storage, refs, index или repository configuration. Git environment очищается от переменных, способных перенаправить operation в иной repository или включить внешнее выполнение.

`remote.origin.url` может читаться только локальной allowlisted командой без config includes и немедленно сводится к SHA-256 в repository fingerprint. Persistent state MUST хранить hash, а не raw origin URL. Эти наблюдения MUST NOT выполнять fetch или обращаться к remote.

## 6. Path and secret handling

Все repository-relative paths MUST пройти нормализацию и containment checks. Абсолютные пути, traversal, NUL, выход из root и смена parent/final file identity должны приводить к отказу или безопасной metadata-only классификации. Безопасное чтение regular file использует pre/post identity evidence и no-follow semantics, когда платформа их предоставляет.

Project-KB-owned home, project storage и registry paths MUST быть каноническими, contained, non-redirected и ожидаемого типа. Canonical snapshot MUST быть regular non-redirected file в точном per-project storage. Operator-pinned MCP snapshot также должен быть regular non-redirected file, но не обязан принадлежать managed home.

Final managed generation MUST находиться в deterministic generation root и быть regular non-redirected file с одной hardlink identity; redirected или multiply-linked generation отклоняется.

Известные hard-secret basenames и suffixes MUST классифицироваться до открытия содержимого. Для них разрешена только metadata, без content hash. Явно безопасные template names могут обрабатываться как обычный текст. Эти правила являются bounded policy, а не обещанием обнаружить произвольный секрет по смыслу; Git ignore и population rules продолжают применяться независимо.

Ошибки и пользовательский вывод не должны раскрывать прочитанный secret content. Pinned MCP configuration документируется только именами переменных и placeholder values.

## 7. Capture and canonical publication invariants

Publisher-neutral capture MUST pin-ить project id, PRIMARY workspace identity, normalized root, repository identity и binding generation до чтения. Временный destination MUST быть свежим, принадлежать операции и находиться вне source repository.

Capture может вернуть sealed descriptor только после:

1. получения допустимой Git population;
2. безопасного bounded scan;
3. записи semantic snapshot schema v2;
4. полной snapshot validation и sealing;
5. проверки terminal source evidence;
6. повторного подтверждения active binding.

Canonical `IndexService` MUST публиковать только такой sealed snapshot. Final `kb.sqlite` заменяется атомарно; registry success bookkeeping выполняется после replace, а опубликованный snapshot затем снова сопоставляется с active binding.

До atomic replace любая ошибка MUST сохранять прежние canonical bytes и очищать принадлежащие операции temporary artifacts. Если binding меняется после replace, операция MUST завершиться отказом и новый canonical snapshot MUST считаться quarantined/unusable. В этой точке policy не обещает возврат прежних bytes.

Canonical structural read MUST открывать snapshot read-only, требовать `can_use_snapshot` и перепроверять active binding/snapshot identity на query boundary. Читаемость snapshot не разрешает generic search.

## 8. Registry v4 and active-binding invariants

Текущий registry schema version равен **4**. Registry v4 MUST хранить authoritative physical checkout identity в workspace record. Для каждого проекта MUST существовать ровно один PRIMARY workspace; project root/repository/binding fields являются согласованной compatibility projection.

Registry initialization и migration MUST быть transactional, проверять точную schema и завершаться отказом при неоднозначной или несовместимой identity. Запись выполняется короткими `BEGIN IMMEDIATE` transactions с foreign-key enforcement. Registry path должен быть regular non-redirected file внутри managed home.

Active binding определяется совокупностью project id, PRIMARY workspace, normalized root, repository identity и binding generation. `relink` MUST создавать новую binding generation, даже если root совпадает с ранее использованным, и MUST переводить canonical snapshot в состояние reindex required. Relink при открытой lifecycle work или unresolved operation должен отказываться.

Canonical capture, publication bookkeeping и canonical read MUST совпадать с active binding. Binding mismatch делает snapshot недоступным или несовместимым независимо от корректности его SQLite schema. `unregister` MUST использовать identity/CAS guard и ограничивать удаление точным per-project storage root; hard removal при lifecycle records не допускается.

## 9. Managed generation publication invariants

Managed generations принадлежат internal lifecycle lane и MUST оставаться отделены от canonical `kb.sqlite`. Canonical index не создаёт lifecycle records; lifecycle publication не заменяет canonical file, не изменяет source repository и не использует canonical run ownership.

Generation publication MUST быть file-first:

1. operation резервирует identity и deterministic final path;
2. sealed temporary snapshot проверяется по schema, snapshot id, размеру, hash и witness;
3. final file создаётся один раз без overwrite;
4. final bytes и identity проверяются повторно;
5. только затем registry transaction регистрирует AVAILABLE generation, обновляет ожидаемый pointer через CAS и завершает operation.

Существующий final path с иной identity, hardlink/redirect, hash mismatch или pointer CAS conflict MUST приводить к отказу. Если файл уже опубликован, а registry finalization не может безопасно завершиться, состояние должно быть зарегистрировано как orphaned/fail-closed; перезапись или незаметный rollback запрещены.

Selector alias MUST сначала разрешаться в точный snapshot id. Exact-generation read MUST повторно проверить registry record, regular/single-link file identity, размер, SHA-256 и snapshot metadata. После смены active binding historical exact id может оставаться явно адресуемым, но alias/currentness не должны молча переноситься на новую binding.

## 10. Currentness and verification boundaries

Currentness — отдельная ось, не синоним availability. Допустимые результаты: `CURRENT`, `STALE`, `UNVERIFIED`, `CHANGED_DURING_CHECK` и `ERROR`.

Режим `fast` удалён: запрос такого режима MUST вернуть `UNVERIFIED` с причиной `fast_verification_removed` и без `verified_at`. Strong verification использует bounded attempts, стабильные observations, snapshot proof и binding checks. Только `CURRENT` разрешает truth claim `CURRENT_AT_VERIFIED_TIME`.

`CURRENT` действует как утверждение о моменте `verified_at` и MUST NOT трактоваться как бессрочная freshness guarantee. Изменение source или binding во время проверки приводит к `CHANGED_DURING_CHECK`, `STALE` либо fail-closed error в зависимости от evidence.

Совместимый `UNVERIFIED` или `STALE` snapshot может оставаться структурно читаемым, поэтому `can_use_snapshot=true` не означает currentness. Операция, требующая текущий snapshot, MUST отдельно применить gate `SNAPSHOT_CURRENT`. Ни availability, ни currentness не включают `can_search`, `can_generate_exports` или `can_generate_context`.

## 11. Frozen MCP transport safety

`pkb-mcp` MUST получить полный pinned identity contract через следующие переменные окружения:

```text
PKB_MCP_SNAPSHOT_PATH=<snapshot-path>
PKB_MCP_PROJECT_ID=<project-id>
PKB_MCP_SNAPSHOT_ID=<snapshot-id>
PKB_MCP_SNAPSHOT_SHA256=<64-hex-sha256>
PKB_MCP_SOURCE_COMMIT=<git-commit>
```

Отсутствующая или некорректная переменная должна остановить bootstrap. При bootstrap server MUST ровно один раз вычислить SHA-256 всего файла и проверить ожидаемые hash, project id, snapshot id, source commit и semantic snapshot schema. Этот полный hash не вычисляется заново для каждого query.

Каждый структурный data query MUST открывать отдельное SQLite connection с `mode=ro`, `immutable=1` и `PRAGMA query_only = ON`, устанавливать bounded VM execution и повторно проверять snapshot id при setup. Metadata tool использует закреплённый bootstrap state без нового SQLite query. Records, filters, values, paths, cursor и response bytes ограничиваются; datasets, columns и filters берутся только из allowlist.

MCP tools являются read-only и MUST NOT публиковать файлы, изменять SQLite, разрешать live registry, выбирать latest generation, менять pointer или заявлять currentness. Read-only annotations дополняют, но не заменяют фактические SQLite/query bounds. Между запросами server доверяет закреплённому immutable artifact contract; per-query full-file rehash не обещан.

## 12. Failure and refusal rules

| Условие | Обязательная реакция |
| --- | --- |
| Операция чтения требует существующий registry, но он отсутствует, имеет неверную schema или ambiguous identity | Отказ без частичной миграции/записи; явная инициализация через `register` остаётся отдельной операцией |
| PRIMARY workspace, repository identity или binding не совпадают | Snapshot недоступен; publication/read не продолжаются |
| Source или binding меняются во время capture/verification | Повтор в пределах bound либо `CHANGED_DURING_CHECK`/отказ |
| Path выходит из root, перенаправлен или меняет identity | Не читать target content; отказ или metadata-only classification |
| Path распознан как hard secret | Не открывать содержимое и не вычислять content hash |
| Capture/build/seal ошибается до canonical replace | Очистить owned temporary artifacts; сохранить прежний canonical file |
| Binding меняется после canonical replace | Отказ и logical quarantine нового snapshot; прежние bytes не гарантируются |
| Generation final path занят, hash/identity не совпадает или CAS проигран | Не перезаписывать; fail closed, при необходимости orphaned state |
| MCP config, bootstrap hash или pinned metadata не совпадают | Server/query не запускается |
| Structural query нарушает allowlist или resource bound | Детерминированный отказ без расширения доступа |

При отказе сохраняется наиболее узкая доказанная truth claim. Ошибка не повышает availability, currentness или capability.

## 13. Validation obligations

Изменение реализации, затрагивающее эту policy, должно подтверждаться текущими source-level invariants и focused safety tests. Минимальная проверка включает:

- registry schema version и migration/active-binding guards;
- отсутствие записи в source repository и live Git storage;
- secret-before-content, redirect и containment behavior;
- canonical pre-publication preservation и post-publication quarantine boundary;
- file-first/create-once generation publication, hash и pointer CAS;
- раздельные availability/currentness/capability semantics;
- frozen MCP bootstrap identity, read-only SQLite pragmas, allowlists и bounds;
- точный audit фактически изменённых файлов и отсутствие незаявленных write locations.

Документационное изменение сохраняет этот файл нормативным safety owner, а [Project KB Architecture](ARCHITECTURE.md) — владельцем current-state component map и runtime flows. Примеры конфигурации используют placeholders и не содержат локальные пользовательские пути или secret values.

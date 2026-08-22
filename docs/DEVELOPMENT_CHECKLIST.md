# Project KB Development Checklist

## 1. How to use this checklist

Это стабильный список gates для разработки и публикации, а не журнал работ и не
историческая матрица. Не каждый пункт применим к каждому изменению: перед принятием
изменения отметьте все затронутые gates и зафиксируйте неприменимые границы явно.

Технические владельцы деталей — [Architecture](ARCHITECTURE.md) и
[Data Safety Policy](DATA_SAFETY_POLICY.md). Статус frozen означает, что новый
продуктовый scope требует отдельного явного решения; его нельзя добавлять незаметно
через расширение этого checklist.

## 2. Before changing the product

- [ ] Определена точная цель изменения и перечислены затронутые публичные и внутренние
  поверхности.
- [ ] Подтверждено, что изменение относится к сопровождению frozen Project KB, а не
  расширяет продуктовый scope.
- [ ] В [Architecture](ARCHITECTURE.md) найден текущий компонент и владелец
  затрагиваемой ответственности.
- [ ] В [Data Safety Policy](DATA_SAFETY_POLICY.md) найдена применимая нормативная
  граница записи, чтения, identity или currentness.
- [ ] Неподдерживаемые capability flags сохраняются выключенными, пока поддержка не
  одобрена и не реализована отдельно.
- [ ] В изменение не смешаны несвязанный cleanup, benchmark work и продуктовые
  изменения.
- [ ] Публичный результат не зависит от приватного пути, локальной среды или
  непереносимого пользовательского предположения.

## 3. Storage, identity and currentness

- [ ] Source repository остаётся read-only входом: продукт не изменяет worktree,
  live Git storage или target code и не выполняет код целевого проекта.
- [ ] Тесты и ручная проверка, способные менять state, используют отдельный
  `PROJECT_KB_HOME` и безопасный тестовый repository, а не реальные registration и
  managed storage пользователя.
- [ ] Canonical project identity и registry binding через PRIMARY workspace,
  repository identity и binding generation остаются явно связанными и проверяемыми.
- [ ] Canonical snapshot проходит capture, validation и sealing до атомарной
  публикации; generation/lifecycle ownership не обходится, а managed generations
  остаются отдельными create-once artifacts.
- [ ] Структурная читаемость snapshot не используется как доказательство currentness:
  availability и currentness проверяются раздельно.
- [ ] `STALE` и `UNVERIFIED` не представляются как `CURRENT`; `CURRENT` относится только
  к моменту подтверждённого `verified_at`.
- [ ] Реализованная cross-snapshot lineage внутреннего lifecycle сохраняет согласованные
  task ownership, parent links, generation sequence и pointers и не выдаётся за
  публичную CLI/MCP capability.
- [ ] Secrets, локальные абсолютные пути и несвязанные repository data не попадают в
  durable или публичные artifacts.

Нормативные детали этих инвариантов принадлежат
[Data Safety Policy](DATA_SAFETY_POLICY.md), а не этому checklist.

## 4. CLI and MCP surfaces

### CLI

- [ ] Упаковка по-прежнему устанавливает `pkb`, `project-kb` и `pkb-mcp`; изменение
  любого entrypoint сверено с `pyproject.toml`, source и focused tests.
- [ ] При изменении CLI проверены `uv run pkb --help`,
  `uv run project-kb --help`, help изменённой команды и её source/test contract.
- [ ] `symbols`, `imports` и `inspect` остаются bounded structural queries, отличными
  от неподдерживаемого generic/semantic search; internal lifecycle не получает
  неявную публичную команду.
- [ ] Вывод `capabilities` и availability соответствует реализации: `search`,
  `exports`, `context_packs`, `can_search`, `can_generate_exports` и
  `can_generate_context` остаются выключенными.

### MCP

- [ ] `pkb-mcp` остаётся persistent `stdio` entrypoint и требует полный
  operator-pinned identity contract: path, project ID, snapshot ID, SHA-256 и source
  commit; неполная или противоречивая конфигурация закрывает startup.
- [ ] MCP обслуживает ровно закреплённый snapshot и не выбирает другой artifact,
  `latest` generation или live registry state.
- [ ] Структурные data queries открывают SQLite через `mode=ro`, `immutable=1` и
  `query_only`; transport не публикует snapshots и не меняет pointers или currentness.
- [ ] Tool annotations и фактическое поведение остаются read-only и structural;
  изменение inventory требует синхронного обновления Architecture, README и focused
  MCP tests.

## 5. Validation gates

- [ ] Требование Python `>=3.14`, зависимости в `pyproject.toml` и `uv.lock` согласованы;
  при изменении зависимостей выполнены `uv lock --check` и `uv sync --locked`.
- [ ] Запущены focused tests ответственности, затронутой изменением, без подмены
  реального контракта чрезмерными mocks или fixtures.
- [ ] Полный текущий test suite проходит командой `uv run pytest`.
- [ ] Формат и lint проходят командами `uv run ruff format --check .` и
  `uv run ruff check .`.
- [ ] При изменении entrypoints, CLI или MCP дополнительно проверены help, installed
  scripts и соответствующие CLI/MCP contract tests, включая `pkb-mcp`.
- [ ] Любая state-changing проверка использует изолированное managed storage; source
  repository и реальный Project KB home остаются неизменными.
- [ ] Default validation не выполняет live network/provider calls и не запускает
  исторические benchmark experiments.
- [ ] Выполнены `git diff --check`, просмотр точного diff и итоговая проверка
  `git status` на незаявленный scope.

## 6. Documentation and publication

- [ ] При изменении публичного поведения или контракта обновлён его canonical owning
  document.
- [ ] README остаётся кратким positioning/navigation документом и не дублирует
  техническое руководство.
- [ ] [Architecture](ARCHITECTURE.md) остаётся владельцем текущих components, flows и
  ownership boundaries.
- [ ] [Data Safety Policy](DATA_SAFETY_POLICY.md) остаётся владельцем нормативных
  write/read, identity и currentness invariants.
- [ ] [Benchmark Methodology](BENCHMARK.md) владеет методикой,
  [Experiments and Results](EXPERIMENTS_AND_RESULTS.md) — измерениями и errata, а
  [Development History](DEVELOPMENT_HISTORY.md) — curated historical lineage.
- [ ] Публичные документы не содержат служебные пути автоматизации, локальные
  абсолютные пути, secrets, приватные значения среды или preservation locations.
- [ ] Все добавленные и затронутые публичные ссылки разрешаются в существующие файлы
  или проверенные внешние цели.
- [ ] Frozen portfolio/reference positioning остаётся точным, а неподдерживаемые
  search, export, context и public lifecycle capabilities не рекламируются.
- [ ] Official release или license readiness заявляется только после появления
  соответствующих metadata, license и подтверждённой public hygiene.

## 7. Historical experiment boundary

- [ ] Canonical historical evaluation не переписана и не заменена задним числом.
- [ ] Diagnostics и errata остаются явно отделены от canonical results и не называются
  новой официальной evaluation.
- [ ] Benchmark runs не входят в обычную contributor/product validation.
- [ ] Документация не утверждает существование formal N5 или G3: N5 не была завершена,
  а G3 не создавался.
- [ ] Остановленная expansion остаётся остановленной, пока отдельно не одобрена новая
  experimental generation.
- [ ] Исторические наблюдения остаются ограниченными конкретными tasks и environment;
  current product acceptance не выводится из одной score table.

Подробные границы принадлежат [Benchmark Methodology](BENCHMARK.md) и
[Experiments and Results](EXPERIMENTS_AND_RESULTS.md); score arrays и методика здесь не
повторяются.

## 8. Before commit or publication

- [ ] Просмотрен точный diff и подтверждён заявленный file scope.
- [ ] В commit или publication не смешаны несвязанные staged, unstaged или untracked
  files.
- [ ] Все применимые focused и default tests, lint и contract checks завершились
  успешно.
- [ ] `git diff --check` завершился без whitespace errors или merge markers.
- [ ] Публичные документы и ссылки соответствуют фактическому product state.
- [ ] В diff нет secrets, локальных путей, приватных values или служебных artifacts.
- [ ] Capability, availability и currentness claims не сильнее доступного evidence.
- [ ] Любое расширение product scope имеет отдельное явное решение и не скрыто внутри
  maintenance change.
- [ ] До заявления official release подтверждены license, publication metadata и
  public hygiene.

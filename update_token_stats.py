#!/usr/bin/env python3
"""
VIBE-CODE Token Statistics Updater
Сканирует все результаты запусков и обновляет статистику использования токенов
"""
import os
import json
import yaml
from datetime import datetime, timedelta
from pathlib import Path

ROOT_DIR = Path(os.environ.get("GITHUB_WORKSPACE", Path(__file__).resolve().parent))
RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", ROOT_DIR / "results"))
TOKEN_USAGE_FILE = Path(os.environ.get("TOKEN_USAGE_FILE", ROOT_DIR / "token_usage.yaml"))
README_FILE = Path(os.environ.get("README_FILE", ROOT_DIR / "README.md"))

def scan_results():
    """Сканирует все папки results и собирает статистику"""
    users_data = {}
    daily_stats = {}
    total_tokens = 0
    total_runs = 0
    
    if not RESULTS_DIR.exists():
        return users_data, daily_stats, total_tokens, total_runs

    for run_folder in RESULTS_DIR.iterdir():
        if not run_folder.is_dir() or run_folder.name.startswith('.'):
            continue
        
        # Пытаемся найти данные о токенах
        tools_status_file = run_folder / "_tools_status.json"
        run_summary = run_folder / "_run_summary.md"
        progress_file = run_folder / "_progress.json"
        
        tokens_used = 0
        timestamp = None
        user = "unknown"
        prompt = ""
        
        # Читаем progress файл если есть
        if progress_file.exists():
            try:
                with open(progress_file) as f:
                    progress = json.load(f)
                    tokens_used = progress.get("tokensUsed", 0)
                    timestamp = progress.get("timestamp")
            except:
                pass
        
        # Пытаемся получить дату из имени папки
        if not timestamp:
            folder_name = run_folder.name
            # Ищем дату в формате YYYYMMDD-HHMMSS
            import re
            match = re.search(r'(\d{8})-(\d{6})', folder_name)
            if match:
                date_str = match.group(1)
                time_str = match.group(2)
                try:
                    timestamp = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T{time_str[:2]}:{time_str[2:4]}:{time_str[4:6]}Z"
                except:
                    timestamp = datetime.now().isoformat() + "Z"
            else:
                timestamp = datetime.now().isoformat() + "Z"
        
        # Пытаемся получить пользователя из run_summary
        if run_summary.exists():
            try:
                with open(run_summary) as f:
                    content = f.read()
                    # Ищем Target Repo
                    import re
                    repo_match = re.search(r'Target Repo\s*\|\s*(\S+)', content)
                    if repo_match:
                        user = repo_match.group(1).split('/')[0]
                    # Ищем prompt
                    prompt_match = re.search(r'\*\*Prompt\*\*\s*\|\s*(.+)', content)
                    if prompt_match:
                        prompt = prompt_match.group(1).strip()
            except:
                pass
        
        # Оцениваем токены если не найдены
        if tokens_used == 0:
            output_file = run_folder / "output.py"
            if output_file.exists():
                with open(output_file) as f:
                    content = f.read()
                    # Грубая оценка: ~4 символа = 1 токен
                    tokens_used = len(content) // 4
        
        # Не подставляем выдуманное значение: если точного счётчика и
        # генерированного файла нет, запуск учитывается с нулём токенов.
        tokens_used = max(0, int(tokens_used or 0))
        
        total_tokens += tokens_used
        total_runs += 1
        
        # Обновляем данные пользователя
        if user not in users_data:
            users_data[user] = {
                "total_tokens": 0,
                "total_runs": 0,
                "last_run": timestamp,
                "runs": []
            }
        
        users_data[user]["total_tokens"] += tokens_used
        users_data[user]["total_runs"] += 1
        users_data[user]["last_run"] = timestamp
        users_data[user]["runs"].append({
            "run_id": run_folder.name,
            "tokens": tokens_used,
            "timestamp": timestamp,
            "prompt": prompt[:100] if prompt else "No prompt"
        })
        
        # Обновляем ежедневную статистику
        date_str = timestamp[:10] if timestamp else datetime.now().strftime("%Y-%m-%d")
        if date_str not in daily_stats:
            daily_stats[date_str] = {"tokens": 0, "runs": 0, "users": set()}
        daily_stats[date_str]["tokens"] += tokens_used
        daily_stats[date_str]["runs"] += 1
        daily_stats[date_str]["users"].add(user)
    
    return users_data, daily_stats, total_tokens, total_runs

def update_yaml(users_data, daily_stats, total_tokens, total_runs):
    """Обновляет YAML файл со статистикой"""
    # Сортируем daily stats по дате
    sorted_dates = sorted(daily_stats.keys())
    
    # Генерируем данные для графика (последние 30 дней)
    graph_labels = []
    graph_tokens = []
    graph_runs = []
    graph_avg = []
    
    for date in sorted_dates[-30:]:
        graph_labels.append(date[5:])  # MM-DD формат
        graph_tokens.append(daily_stats[date]["tokens"])
        graph_runs.append(daily_stats[date]["runs"])
        avg = daily_stats[date]["tokens"] // max(daily_stats[date]["runs"], 1)
        graph_avg.append(avg)
    
    # Топ пользователей
    top_users = sorted(
        [{"login": k, "tokens": v["total_tokens"], "runs": v["total_runs"]} 
         for k, v in users_data.items()],
        key=lambda x: x["tokens"],
        reverse=True
    )[:10]
    
    # Вычисляем totals
    weekly_tokens = sum(daily_stats[d]["tokens"] for d in sorted_dates[-7:] if d in daily_stats)
    monthly_tokens = sum(daily_stats[d]["tokens"] for d in sorted_dates[-30:] if d in daily_stats)
    
    yaml_content = f"""# Token Usage Tracking — All Users
# Auto-generated by update_token_stats.py
# Last updated: {datetime.now().isoformat()}Z

# Global statistics
global_total_tokens: {total_tokens}
global_total_runs: {total_runs}
first_run_date: "{sorted_dates[0] if sorted_dates else datetime.now().strftime('%Y-%m-%d')}"
last_updated: "{datetime.now().isoformat()}Z"

# Per-user token usage
users:
"""
    
    for login, data in sorted(users_data.items(), key=lambda x: x[1]["total_tokens"], reverse=True):
        yaml_content += f"""  "{login}":
    total_tokens: {data["total_tokens"]}
    total_runs: {data["total_runs"]}
    last_run: "{data["last_run"]}"
    runs:
"""
        for run in data["runs"][-5:]:  # Последние 5 запусков
            yaml_content += f"""      - run_id: "{run['run_id']}"
        tokens: {run['tokens']}
        timestamp: "{run['timestamp']}"
        prompt: "{run['prompt'][:50]}"
"""
    
    yaml_content += f"""
# Daily aggregation for graphs
daily_stats:
"""
    for date in sorted_dates[-30:]:
        users_list = list(daily_stats[date]["users"])
        yaml_content += f"""  "{date}":
    tokens: {daily_stats[date]["tokens"]}
    runs: {daily_stats[date]["runs"]}
    users: {json.dumps(users_list)}
"""
    
    yaml_content += f"""
# Weekly/Monthly/Yearly totals
weekly_total: {weekly_tokens}
monthly_total: {monthly_tokens}
yearly_total: {monthly_tokens}

# Graph data points (last 30 days)
graph_data:
  labels: {json.dumps(graph_labels)}
  tokens: {json.dumps(graph_tokens)}
  runs: {json.dumps(graph_runs)}
  avg_tokens: {json.dumps(graph_avg)}

# Top users by tokens (auto-sorted)
top_users:
"""
    for u in top_users:
        yaml_content += f"""  - login: "{u['login']}"
    tokens: {u['tokens']}
    runs: {u['runs']}
"""
    
    with open(TOKEN_USAGE_FILE, 'w', encoding='utf-8') as f:
        f.write(yaml_content)
    
    print(f"✓ Updated {TOKEN_USAGE_FILE}")
    print(f"  Total tokens: {total_tokens:,}")
    print(f"  Total runs: {total_runs:,}")
    print(f"  Users: {len(users_data)}")

def update_readme():
    """Обновляет README с графиком и таблицей"""
    try:
        with open(TOKEN_USAGE_FILE, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
    except:
        print("⚠ Could not load token_usage.yaml")
        return
    
    graph_data = data.get('graph_data', {})
    top_users = data.get('top_users', [])
    daily_stats = data.get('daily_stats', {})
    
    # Генерируем ASCII график для README
    labels = graph_data.get('labels', [])
    tokens = graph_data.get('tokens', [])
    
    if tokens:
        max_tokens = max(tokens)
        chart_lines = []
        for i, (label, tok) in enumerate(zip(labels[-14:], tokens[-14:])):  # Последние 14 дней
            bar_len = int((tok / max_tokens) * 40) if max_tokens > 0 else 0
            bar = '█' * bar_len
            chart_lines.append(f"`{label}` │ {bar} {tok:,}")
        
        chart_md = "\n".join(chart_lines)
    else:
        chart_md = "*Нет данных*"
    
    # Генерируем таблицу топ пользователей
    users_table = "| # | Пользователь | Токены | Запуски |\n|---|--------------|--------|---------|\n"
    for i, u in enumerate(top_users[:10], 1):
        users_table += f"| {i} | `{u['login']}` | {u['tokens']:,} | {u['runs']} |\n"
    
    # Читаем текущий README
    with open(README_FILE, 'r', encoding='utf-8') as f:
        readme_content = f.read()
    
    # Находим секцию для статистики или добавляем новую
    stats_section = f"""
---
## 📊 Статистика использования токенов

_Данные автоматически обновляются при каждом запуске_

### График потребления токенов (последние 14 дней)

```
{chart_md}
```

### Топ пользователей

{users_table}

### Общая статистика

- **Всего токенов использовано:** {data.get('global_total_tokens', 0):,}
- **Всего запусков:** {data.get('global_total_runs', 0)}
- **За неделю:** {data.get('weekly_total', 0):,} токенов
- **За месяц:** {data.get('monthly_total', 0):,} токенов

<details>
<summary><strong>📅 Детальная статистика по дням</strong></summary>

| Дата | Токены | Запуски | Пользователей |
|------|--------|---------|---------------|
"""
    
    for date in sorted(daily_stats.keys())[-14:]:
        d = daily_stats[date]
        users_count = len(d.get('users', []))
        stats_section += f"| {date} | {d['tokens']:,} | {d['runs']} | {users_count} |\n"
    
    stats_section += """
</details>
"""
    
    # Проверяем есть ли уже секция статистики
    if "## 📊 Статистика использования токенов" in readme_content:
        # Заменяем существующую секцию
        import re
        pattern = r'## 📊 Статистика использования токенов.*?(?=---|$)'
        readme_content = re.sub(pattern, stats_section.strip(), readme_content, flags=re.DOTALL)
    else:
        # Добавляем перед последним разделом
        readme_content = readme_content.replace("</div>\n\n", stats_section + "\n</div>\n\n")
    
    with open(README_FILE, 'w', encoding='utf-8') as f:
        f.write(readme_content)
    
    print("✓ Updated README.md with token statistics")

if __name__ == "__main__":
    print("🔍 Scanning results...")
    users_data, daily_stats, total_tokens, total_runs = scan_results()
    
    print("📝 Updating token_usage.yaml...")
    update_yaml(users_data, daily_stats, total_tokens, total_runs)
    
    print("📖 Updating README.md...")
    update_readme()
    
    print("\n✅ Done!")

from telebot.infrastructure.persistence import SqliteReportRepository

def main():
    repo = SqliteReportRepository()
    reports = repo.get_latest_reports(5)
    
    if not reports:
        print("No reports found in the database.")
        return
        
    print(f"Found {len(reports)} reports in the database:")
    for r in reports:
        print(f"--- Report ID: {r['id']} ---")
        print(f"Date: {r['date']}")
        print(f"Task: {r['task_name']}")
        print(f"Channel: {r['channel_id']}")
        print(f"MD Path: {r['md_path']}")
        print(f"PDF Path: {r['pdf_path']}")
        print(f"Summary Start: {r['summary'][:100]}...")
        print()

if __name__ == "__main__":
    main()

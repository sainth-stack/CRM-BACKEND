from app.db.database import engine
from sqlalchemy import inspect
import os

def check_db():
    print(f"Connecting to: {os.getenv('NEON_DB_URL')}")
    try:
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        print(f"Tables found: {tables}")
        
        if not tables:
            print("No tables found. Attempting to create them now...")
            from app.db.models import Base
            Base.metadata.create_all(bind=engine)
            print("Tables created successfully.")
            tables = inspector.get_table_names()
            print(f"New tables found: {tables}")
        
    except Exception as e:
        print(f"Error connecting to database: {e}")

if __name__ == "__main__":
    check_db()

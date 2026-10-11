# Employee Attendance API
A FastAPI-based employee attendance management system using MongoDB. It supports employee management, punch-in/punch-out, attendance regularization with audit history, and analytics for monthly attendance, department summaries, late-arrival rankings, and attendance trends.

## Tech Stack
- Python, FastAPI, Uvicorn
- MongoDB, PyMongo
- Pydantic, python-dotenv

## Setup
1. Install dependencies:
   `pip install -r requirements.txt`
2. Configure MongoDB in your `.env` file.
3. Start the server:
   `uvicorn app.main:app --reload --port 8000`
4. Open `http://127.0.0.1:8000/docs` to explore and test the API.

Ensure MongoDB is running before testing endpoints.
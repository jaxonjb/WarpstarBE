FROM python:3.10-slim
 
# Set working directory
WORKDIR /app
 
# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
 
# Copy application code
COPY . .
 
# Expose port (Railway sets $PORT automatically)
EXPOSE 8000
 
# Start the server — Railway injects $PORT at runtime
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2"]
# Use an official Python runtime 
FROM python:3.13.3-slim

# Set the working directory inside the container
WORKDIR /usr/src/app

# Copy requirements.txt first for better caching
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy local code to container image
COPY . ./

# Set environment variables
EXPOSE 8080

# Start the Gunicorn server 
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "4", "--threads", "8", "--timeout", "0", "app:app"]
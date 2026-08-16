"""FastAPI application entry point"""
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="MedicaAI MVP",
    description="Medical AI Assistant API",
    version="0.1.0"
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure based on your needs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Import routers here
# from app.api import health, agents, twilio_routes
# app.include_router(health.router)
# app.include_router(agents.router)
# app.include_router(twilio_routes.router)


@app.get("/")
async def root():
    """Root endpoint"""
    return {"message": "MedicaAI MVP API is running"}


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

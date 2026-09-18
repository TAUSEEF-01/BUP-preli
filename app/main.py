"""FastAPI service: GET /health and POST /optimize-energy."""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings, load_settings
from app.llm.interpreter import InterpretationError, Interpreter
from app.optimizer import SolverError
from app.pipeline import PipelineError, run_pipeline
from app.request_validation import RequestValidationError, parse_scenario
from app.schemas import BatteryInput, HourInput, Scenario

logger = logging.getLogger("gridwise")

GENERIC_500 = "The request could not be processed. Please retry later."
ERROR_CODES = {400: "bad_request", 404: "not_found", 405: "method_not_allowed",
               422: "unprocessable_entity", 500: "internal_error"}


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": ERROR_CODES.get(status, "error"), "message": message})


def _warmup_scenario() -> Scenario:
    hours = tuple(HourInput(h, 0.0, 0.0, 1.0) for h in range(24))
    battery = BatteryInput(100.0, 50.0, 10.0, 10.0, 10.0)
    return Scenario("warmup", ("The cafeteria menu changes tomorrow.",), hours, battery)


async def _warmup(interpreter: Interpreter) -> bool:
    """Pay one-time costs (connection setup, schema compilation) before the first real request."""
    try:
        await interpreter.interpret(_warmup_scenario(), time.monotonic() + 30)
        logger.info("LLM warm-up completed")
        return True
    except Exception as exc:
        logger.warning("LLM warm-up failed: %s", type(exc).__name__)
        return False


def create_app(settings: Settings | None = None, interpreter: Interpreter | None = None) -> FastAPI:
    settings = settings or load_settings()
    logging.basicConfig(level=settings.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for problem in settings.config_errors:
            logger.error("configuration: %s", problem)
        app.state.interpreter = interpreter or Interpreter.from_settings(settings)
        app.state.ready = False
        if not app.state.interpreter.configured:
            logger.error("no LLM provider configured: service is not ready")
        elif settings.config_errors:
            logger.error("invalid configuration: service is not ready")
        elif settings.warmup:
            # Warm up before readiness so it cannot occupy the LLM semaphore behind a 200
            # health response and delay the first judge request.
            app.state.ready = await _warmup(app.state.interpreter)
        else:
            # Useful for deterministic tests and providers where a paid warm-up is undesirable.
            # This verifies configuration only, not remote credentials.
            app.state.ready = True
        yield
        await app.state.interpreter.aclose()

    app = FastAPI(title="GridWise LLM", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return _error(exc.status_code, message)

    @app.exception_handler(Exception)
    async def unhandled_error(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error: %s", type(exc).__name__, exc_info=True)
        return _error(500, GENERIC_500)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        if request.app.state.ready:
            return JSONResponse(content={"status": "ok"})
        return JSONResponse(status_code=503, content={"status": "error"})

    @app.post("/optimize-energy")
    async def optimize_energy(request: Request) -> JSONResponse:
        request_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        body = await request.body()
        if len(body) > settings.max_body_bytes:
            return _error(400, "Request body is too large")
        try:
            scenario = parse_scenario(body)
        except RequestValidationError as exc:
            logger.info("request=%s rejected status=%s: %s", request_id, exc.status_code, exc.message)
            return _error(exc.status_code, exc.message)

        try:
            result = await run_pipeline(scenario, request.app.state.interpreter, settings)
        except InterpretationError as exc:
            logger.error("request=%s scenario=%s interpretation failed: %s",
                         request_id, scenario.scenario_id, exc)
            return _error(500, GENERIC_500)
        except (PipelineError, SolverError) as exc:
            logger.error("request=%s scenario=%s pipeline failed: %s",
                         request_id, scenario.scenario_id, exc)
            return _error(500, GENERIC_500)

        logger.info(
            "request=%s scenario=%s status=200 source=%s cache_hit=%s timings_ms=%s total_ms=%.0f",
            request_id, scenario.scenario_id, result.source, result.cache_hit,
            {k: round(v) for k, v in result.timings_ms.items()}, (time.monotonic() - started) * 1000,
        )
        return JSONResponse(content=result.response)

    return app


app = create_app()

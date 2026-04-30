# CLAUDE.md - Energy Orchestrator Configuration & Context

## Project Overview

The Energy Orchestrator is a mission-critical Python service that optimizes home electricity costs on Belgian dynamic tariffs through intelligent SolarEdge inverter control. The system integrates multiple energy devices, applies rule-based decision logic, and provides comprehensive monitoring and control interfaces.

## Architecture & Design Patterns

### Layered Architecture
- **Presentation Layer**: FastAPI web interface with RESTful APIs
- **Business Logic Layer**: Rule engine with pluggable decision strategies
- **Data Access Layer**: Repository pattern with async SQLAlchemy
- **Infrastructure Layer**: Device communication adapters and external integrations

### Key Design Patterns
- **Strategy Pattern**: Pluggable decision rules and data source adapters
- **Repository Pattern**: Database abstraction with unit of work
- **Factory Pattern**: Device client instantiation based on configuration
- **Observer Pattern**: Event-driven notifications for state changes
- **Circuit Breaker Pattern**: Fault tolerance for external service calls

### Dependency Injection
- Use dependency injection container for loose coupling
- Interface-based programming for testability
- Configuration-driven service registration
- Scoped lifetimes for database connections and HTTP clients

## Technology Stack & Dependencies

### Core Framework Stack
```yaml
# Web Framework & Server
fastapi: "^0.104.0"
uvicorn: "^0.24.0"
pydantic: "^2.5.0"
pydantic-settings: "^2.1.0"

# Database & ORM
sqlalchemy: "^2.0.23"
aiosqlite: "^0.19.0"
alembic: "^1.13.0"

# Communication Protocols
aiohttp: "^3.9.0"
pymodbus: "^3.6.0"
websockets: "^12.0"

# Utilities & Infrastructure
structlog: "^23.2.0"
tenacity: "^8.2.0"
pyyaml: "^6.0.1"
jinja2: "^3.1.2"
python-multipart: "^0.0.6"
```

### Development & Testing Dependencies
```yaml
# Testing Framework
pytest: "^7.4.0"
pytest-asyncio: "^0.21.0"
pytest-mock: "^3.12.0"
httpx: "^0.25.0"

# Code Quality
black: "^23.11.0"
ruff: "^0.1.6"
mypy: "^1.7.0"
pre-commit: "^3.5.0"

# Documentation
mkdocs: "^1.5.0"
mkdocs-material: "^9.4.0"
```

## Configuration Management Architecture

### Configuration Window Design

#### Primary Configuration Interface
Create a modern, tabbed configuration interface using a lightweight GUI framework (tkinter with ttk styling or PyQt6):

**Tab 1: Device Configuration**
- **sonnenBatterie Section**
  - IP address validation with network reachability test
  - Port configuration (80/8080) with auto-detection
  - API version selection (v1/v2) with capability detection
  - Secure token storage with encryption at rest
  - Battery capacity (kWh) with validation against detected specs
  
- **HomeWizard Devices Section**
  - Device discovery via mDNS/SSDP scanning
  - Individual device configuration cards with status indicators
  - Power threshold configuration with real-time validation
  - Device-specific calibration settings

- **SolarEdge Integration Section**
  - Modbus TCP connection parameters with protocol testing
  - Register mapping validation against inverter model
  - Safety limits and emergency shutdown configuration
  - Firmware compatibility checking

**Tab 2: Decision Logic Configuration**
- **Battery Management**
  - SoC thresholds with visual range indicators
  - Hysteresis configuration with stability analysis
  - Charging/discharging rate limits
  
- **Pricing Strategy**
  - Dynamic pricing provider configuration
  - Price threshold matrices for different scenarios
  - Forecast accuracy tracking and adjustment
  
- **Safety & Override Controls**
  - Emergency stop conditions
  - Manual override timeout policies
  - Fail-safe mode configuration

**Tab 3: System & Monitoring**
- **Operational Parameters**
  - Polling intervals with performance impact analysis
  - Database maintenance schedules
  - Health check configuration
  
- **Logging & Alerting**
  - Log level configuration per component
  - Alert threshold management
  - Notification channel setup (email, webhook)

**Tab 4: Validation & Deployment**
- **Configuration Testing**
  - Comprehensive connectivity testing suite
  - Configuration validation with detailed reports
  - Dry-run mode simulation with historical data
  
- **Backup & Recovery**
  - Configuration versioning and rollback
  - Export/import with encryption
  - Disaster recovery procedures

### Configuration Data Model

```python
# Use Pydantic v2 for robust configuration validation
from pydantic import BaseModel, Field, validator
from pydantic_settings import BaseSettings
from typing import Optional, Dict, List
from enum import Enum

class DeviceConfig(BaseModel):
    host: str = Field(..., description="Device IP address")
    port: int = Field(default=80, ge=1, le=65535)
    timeout_s: float = Field(default=5.0, gt=0, le=30)
    retry_count: int = Field(default=3, ge=1, le=10)
    
class SonnenBatterieConfig(DeviceConfig):
    api_version: Literal["v1", "v2"] = "v2"
    auth_token: Optional[str] = None
    capacity_kwh: float = Field(..., gt=0, description="Battery capacity in kWh")
```

## Data Source Integration Architecture

### Device Communication Pattern

**Abstract Device Interface**
```python
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime

@dataclass
class DeviceReading:
    timestamp: datetime
    device_id: str
    data: Dict[str, Any]
    quality: float  # 0.0 to 1.0, data quality indicator
    
class DeviceClient(ABC):
    @abstractmethod
    async def read_data(self) -> Optional[DeviceReading]:
        """Read current data from device"""
        pass
    
    @abstractmethod
    async def health_check(self) -> bool:
        """Verify device connectivity and status"""
        pass
```

### sonnenBatterie Integration

**Implementation Requirements:**
- **Multi-API Support**: Automatic fallback from v2 to v1 API
- **Authentication**: Secure token management with renewal handling
- **Data Validation**: Schema validation against known API responses
- **Performance**: Connection pooling and keep-alive optimization
- **Monitoring**: Detailed metrics on API response times and reliability

**Key Metrics Processing:**
- USOC normalization (0-100% validation)
- Power direction logic (positive/negative interpretation)
- Production/consumption correlation analysis
- Grid feed-in accuracy validation

### HomeWizard Device Integration

**Discovery & Management:**
- **Auto-Discovery**: Network scanning for HomeWizard devices
- **Device Identification**: Automatic device type detection
- **Calibration**: Power meter accuracy calibration procedures
- **Redundancy**: Multiple device support for critical measurements

**Device-Specific Logic:**
- **Car Charger**: Dynamic threshold adjustment based on charging patterns
- **P1 Meter**: Belgian smart meter protocol compliance
- **Solar Meter**: Irradiance correlation and weather integration

### SolarEdge Modbus Control

**Safety-First Design:**
- **Connection Validation**: Pre-flight checks before any write operations
- **Command Verification**: Read-back confirmation of all control commands
- **Emergency Stops**: Multiple failsafe mechanisms
- **Rate Limiting**: Command frequency limits to protect inverter

**Advanced Features:**
- **Gradual Ramping**: Smooth power transitions to avoid grid disturbances
- **Status Monitoring**: Continuous inverter health monitoring
- **Fault Detection**: Automatic detection of inverter faults or disconnections

### ENTSO-E Price Data Integration

**Multi-Source Strategy:**
- **Primary**: ENTSO-E Transparency Platform with authentication
- **Secondary**: Supplier APIs (Tibber, energy providers)
- **Fallback**: Local price database with historical patterns
- **Development**: CSV import for testing scenarios

**Price Data Processing:**
- **Validation**: Price reasonability checks and outlier detection
- **Interpolation**: Gap filling for missing hourly data
- **Forecasting**: Short-term price prediction using historical patterns
- **Alerting**: Notifications for unusual price conditions

## Logging & Monitoring Architecture

### Structured Logging Framework

**Log Structure Design:**
```python
import structlog
from typing import Dict, Any
from datetime import datetime

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.add_timestamp,
        structlog.processors.CallsiteParameterAdder(),
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
)
```

**Log Categories & Levels:**
- **CRITICAL**: Service failures, safety violations, data corruption
- **ERROR**: Device communication failures, decision logic errors
- **WARNING**: Performance degradation, configuration issues, retries
- **INFO**: Normal operations, state changes, successful operations
- **DEBUG**: Detailed execution traces, raw data dumps, performance metrics

### Logger Configuration Interface

**Real-Time Log Viewer:**
- **Filtering**: Multi-dimensional filtering (time, level, component, device)
- **Search**: Full-text search with regex support
- **Export**: Selective log export with format options
- **Performance**: Efficient handling of high-volume log streams

**Log Management Features:**
- **Rotation**: Size-based and time-based rotation policies
- **Compression**: Automatic compression of archived logs
- **Retention**: Configurable retention with storage optimization
- **Monitoring**: Log volume analysis and anomaly detection

### Metrics & Performance Monitoring

**Key Performance Indicators:**
- **Response Times**: Per-device API response time percentiles
- **Success Rates**: Device connectivity and command success rates
- **Decision Accuracy**: Rule engine performance metrics
- **System Resources**: CPU, memory, disk usage monitoring

**Dashboard Integration:**
- **Real-Time Metrics**: Live system performance indicators
- **Historical Analysis**: Long-term trend analysis and capacity planning
- **Alerting**: Proactive alerts for performance degradation
- **Reporting**: Automated performance reports and insights

## Dashboard Visualizations

Per the project spec, the dashboard at ``/`` must show two time-series charts
in addition to the live tiles:

- **Day-ahead injection price bar chart** — 24 hours of hourly bars (today,
  plus tomorrow when ENTSO-E has published it). The current hour is highlighted
  and negative-price hours are coloured distinctly. Data source: the
  orchestrator's price cache (populated by the tick loop), exposed via the
  ``/api/prices`` endpoint.
- **24-hour SoC + injection-price overlay** — battery SoC line chart with
  injection price as a secondary series, and ON/OFF zone shading drawn from
  the ``decisions`` table. Data source: ``/api/history?h=24``.

Charting library: a small vendored JS library (Chart.js or similar) shipped
under ``src/energy_orchestrator/web/static/``. **No CDN** — the orchestrator
runs on a home LAN that may not always have outbound internet, so all UI
assets must work offline.

## Development Standards & Best Practices

### Code Organization & Architecture

```
energy_orchestrator/
├── src/
│   ├── config/              # Configuration management
│   ├── devices/             # Device communication clients
│   ├── decision/            # Rule engine and decision logic
│   ├── data/               # Database models and repositories
│   ├── web/                # FastAPI application and routes
│   ├── monitoring/         # Logging and metrics
│   └── utils/              # Shared utilities
├── tests/
│   ├── unit/               # Unit tests
│   ├── integration/        # Integration tests
│   └── fixtures/           # Test data and mocks
├── alembic/                # Database migrations
├── static/                 # Web interface assets
└── docs/                   # Documentation
```

### Code Quality Standards

**Type Safety:**
- Comprehensive type hints using `typing` and `mypy`
- Strict mypy configuration with no implicit anys
- Runtime type validation for external data

**Testing Strategy:**
- **Unit Tests**: >90% code coverage with pytest
- **Integration Tests**: End-to-end device communication testing
- **Property-Based Testing**: Using `hypothesis` for edge case discovery
- **Performance Tests**: Load testing for high-frequency operations
- **Contract Tests**: API compatibility testing

**Code Style & Formatting:**
- **Black**: Consistent code formatting
- **Ruff**: Fast linting with comprehensive rule set
- **Pre-commit hooks**: Automated quality checks
- **Documentation**: Comprehensive docstrings with examples

### Error Handling & Resilience

**Fault Tolerance Patterns:**
```python
from tenacity import retry, stop_after_attempt, wait_exponential
from contextlib import asynccontextmanager

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10)
)
async def resilient_device_read(device_client):
    """Resilient device reading with exponential backoff"""
    pass

@asynccontextmanager
async def circuit_breaker(failure_threshold: int = 5):
    """Circuit breaker pattern for external service calls"""
    pass
```

**Error Recovery Strategies:**
- **Graceful Degradation**: Continue operation with reduced functionality
- **Automatic Recovery**: Self-healing capabilities where possible
- **State Preservation**: Maintain critical state during failures
- **Alert Escalation**: Progressive alert levels based on failure severity

### Security & Safety Implementation

**Data Protection:**
- **Encryption**: AES-256 encryption for sensitive configuration data
- **Access Control**: Role-based access for configuration changes
- **Audit Logging**: Comprehensive audit trail for all modifications
- **Secure Communication**: TLS for all external communications

**Safety Mechanisms:**
- **Input Validation**: Comprehensive validation of all external inputs
- **Rate Limiting**: Protection against excessive API calls
- **Emergency Stops**: Multiple layers of safety shutdowns
- **Sanity Checks**: Continuous validation of system state consistency

### Performance Optimization

**Async Programming Best Practices:**
- Connection pooling for HTTP clients
- Batch operations for database transactions
- Efficient memory management for time-series data
- Optimized JSON parsing and serialization

**Database Performance:**
- Proper indexing strategy for time-series queries
- Partitioning for large datasets
- Query optimization and explain plan analysis
- Connection pooling and transaction management

### Deployment & Operations

**Configuration Management:**
- Environment-specific configuration files
- Configuration validation on startup
- Hot-reload capability for non-critical settings
- Configuration versioning and change tracking

**Monitoring & Observability:**
- Health check endpoints with detailed status
- Prometheus metrics export
- Distributed tracing for complex operations
- Custom dashboards for operational visibility

**Maintenance Procedures:**
- Automated backup and recovery procedures
- Database maintenance and optimization scripts
- Log rotation and cleanup automation
- Update deployment procedures with rollback capability
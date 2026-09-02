FROM continuumio/miniconda3:latest

# System dependencies for CadQuery/OpenCascade and rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-xlib-2.0-0 \
    libosmesa6 \
    libosmesa6-dev \
    libglu1-mesa \
    mesa-utils \
    libgomp1 \
    libtbb-dev \
    libfreeimage3 \
    libfreetype6 \
    libharfbuzz0b \
    && rm -rf /var/lib/apt/lists/* \
    && ldconfig

# Make sure libs are findable everywhere
ENV LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu:/opt/conda/lib:${LD_LIBRARY_PATH}"
ENV LIBGL_ALWAYS_SOFTWARE=1

# Create conda environment with CadQuery (has all native deps bundled)
RUN conda install -c conda-forge -c cadquery python=3.11 cadquery=2.4.0 -y && conda clean -afy

# Symlink system libs into conda lib so OpenCascade always finds them
RUN for lib in libOSMesa.so.8 libOSMesa.so libgomp.so.1; do \
        [ -f /usr/lib/x86_64-linux-gnu/$lib ] && ln -sf /usr/lib/x86_64-linux-gnu/$lib /opt/conda/lib/$lib || true; \
    done && ldconfig

# Python dependencies
RUN pip install --no-cache-dir \
    flask \
    gunicorn \
    cairosvg \
    reportlab \
    matplotlib \
    Pillow

WORKDIR /app

# Copy application files
COPY step_quote_extract.py .
COPY generate_views.py .
COPY render_flat_pattern.py .
COPY generate_report.py .
COPY app.py .
COPY INSTRUCTIONS.md .

# Create upload directory
RUN mkdir -p /tmp/step_uploads

# Expose port
EXPOSE 8080

# Run with gunicorn for production
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "300", "--workers", "2", "app:app"]

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
                                                                && rm -rf /var/lib/apt/lists/* \
                                                                    && ldconfig

                                                                    # Make sure libOSMesa is findable everywhere
                                                                    ENV LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu:/opt/conda/lib:${LD_LIBRARY_PATH}"
                                                                    ENV LIBGL_ALWAYS_SOFTWARE=1

                                                                    # Create conda environment with CadQuery (has all native deps bundled)
                                                                    RUN conda install -c conda-forge -c cadquery python=3.11 cadquery=2.4.0 -y && conda clean -afy

                                                                    # Symlink libOSMesa into conda's lib so OpenCascade always finds it
                                                                    RUN ln -sf /usr/lib/x86_64-linux-gnu/libOSMesa.so.8 /opt/conda/lib/libOSMesa.so.8 || true \
                                                                        && ln -sf /usr/lib/x86_64-linux-gnu/libOSMesa.so /opt/conda/lib/libOSMesa.so || true \
                                                                            && ldconfig

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

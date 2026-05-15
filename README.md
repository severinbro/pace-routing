# PACE: Personalized Assessment of Comfort for Explainable Routing

🚧 Project Status: Under Construction 🚧 
The PACE software ecosystem, including the containerized edge-processing environment and the GraphSAGE data fusion scripts, is currently undergoing final refinement. This repository is being prepared for upcoming empirical field trials.

## Project Overview 🔍
Urban comfort is a multidimensional construct driven by the complex interaction between citizens and their physical environment. Historically, the investigation of urban livability has suffered from a fundamental conceptual conflict: the "city as a machine," which prioritizes objective technological measurements, versus the "city as an organism," which emphasizes subjective socio-psychological perceptions. PACE (Personalized Assessment of Comfort for Explainable Routing) is an open-source software ecosystem and methodological framework designed to bridge this gap. By treating the pedestrian as an active sensor, PACE moves beyond isolated domains of urban study to capture the holistic, lived reality of the urban dweller.

## Core Goals and Architecture 🚶‍♀️🚶
The primary objective of PACE is to successfully translate heterogeneous data streams into a unified, transparent, and highly personalized urban comfort model. To achieve this, the project is structured around three key pillars:
1. **Synchronous Data Acquisition**: Leveraging a custom, backpack-mounted low-cost sensor prototype alongside a localized web application, PACE captures dynamic microclimate data and human thermal memory in real-time during "Urban Comfort Walks". The platform synchronously records 18 objective environmental features—spanning atmospheric, air quality, visual, and spatial data—while simultaneously logging subjective human feedback.
2. **Multimodal Data Fusion Pipeline**: Because bridging the gap between objective telemetry and subjective perception is highly complex, the analytical pipeline utilizes a rigorously defined three-step framework:
  - EW-AHP Triangulation: Identifying perceptual "mismatch zones" where objective environmental indices conflict with subjective human perception.
  - GraphSAGE Neural Networks: Accounting for the non-linear, topological complexities of urban environments to perform robust spatial interpolation across the street network.
  - Explainable AI (GraphLIME): Addressing the "black box" nature of deep learning by mathematically extracting feature importance, revealing exactly which physical or environmental variables drive local comfort predictions.
4. **Comfort-Optimized, Personalized Routing**: The ultimate goal of the PACE framework is to inform future comfort-optimized routing algorithms that prioritize restorative environments over the absolute shortest path. By mapping the non-linear interactions between the physical environment and a specific user's psychological sensitivities, the system generates user-preference weighted routes that individualize the urban experience.

## Repository Contents (Upcoming) 🔧
Once the repository is fully published, it will host:
- The source code for the localized Django web application and survey interface.
- The Raspberry Pi edge-processing and sensor polling scripts (Docker/Gunicorn environment).
- The Python-based GraphSAGE and GraphLIME data fusion pipeline for network spatial interpolation.

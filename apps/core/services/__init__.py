"""Service layer (business logic / the "Model" in the MVC sense).

These classes take explicit parameters and return data.  They never call
``input()``, never print to stdout, and keep heavy/desktop imports lazy so the
package remains importable in a headless web process.
"""

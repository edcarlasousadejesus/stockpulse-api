"""Modelos iniciais por tipo de negócio: insumos, produtos e meta de CMV SUGERIDA (ponto de partida, não regra)."""
from decimal import Decimal as D

# insumo: (nome, unidade, custo)   produto: (nome, categoria, preço, outros custos, [(insumo, quantidade)])
MODELOS: dict[str, dict] = {
    "hamburgueria": {"meta": "28", "ins": [("Carne bovina", "kg", "32"), ("Queijo", "kg", "38"), ("Pão de hambúrguer", "un", "1.20"), ("Refrigerante", "un", "3")],
        "pr": [("Hambúrguer da casa", "Hambúrgueres", "28", "0.86", [("Pão de hambúrguer", "1"), ("Carne bovina", "0.15"), ("Queijo", "0.03")]),
               ("Cheeseburger duplo", "Hambúrgueres", "38", "1.10", [("Pão de hambúrguer", "1"), ("Carne bovina", "0.3"), ("Queijo", "0.06")]),
               ("Refrigerante lata", "Bebidas", "7", "0", [("Refrigerante", "1")])]},
    "restaurante": {"meta": "32", "ins": [("Arroz", "kg", "6.5"), ("Feijão", "kg", "8"), ("Filé de frango", "kg", "18"), ("Carne bovina", "kg", "38"), ("Batata", "kg", "5"), ("Salada mista", "kg", "12"), ("Refrigerante lata", "un", "3")],
        "pr": [("Prato executivo de frango", "Pratos", "29.9", "1.5", [("Arroz", "0.15"), ("Feijão", "0.1"), ("Filé de frango", "0.2"), ("Batata", "0.1"), ("Salada mista", "0.05")]),
               ("Prato executivo de carne", "Pratos", "34.9", "1.5", [("Arroz", "0.15"), ("Feijão", "0.1"), ("Carne bovina", "0.2"), ("Batata", "0.1"), ("Salada mista", "0.05")]),
               ("Refrigerante lata", "Bebidas", "7", "0", [("Refrigerante lata", "1")])]},
    "acai": {"meta": "30", "ins": [("Polpa de açaí", "kg", "22"), ("Granola", "kg", "14"), ("Banana", "kg", "6"), ("Leite condensado", "kg", "18"), ("Morango", "kg", "20"), ("Copo 500 ml", "un", "0.9")],
        "pr": [("Açaí 300 ml", "Açaí", "16", "0.3", [("Polpa de açaí", "0.3"), ("Granola", "0.03"), ("Banana", "0.05"), ("Leite condensado", "0.02"), ("Copo 500 ml", "1")]),
               ("Açaí 500 ml", "Açaí", "24", "0.3", [("Polpa de açaí", "0.5"), ("Granola", "0.05"), ("Banana", "0.08"), ("Leite condensado", "0.03"), ("Copo 500 ml", "1")]),
               ("Açaí com morango 500 ml", "Especiais", "28", "0.3", [("Polpa de açaí", "0.5"), ("Granola", "0.05"), ("Morango", "0.08"), ("Leite condensado", "0.03"), ("Copo 500 ml", "1")])]},
    "bar": {"meta": "26", "ins": [("Cerveja long neck", "un", "4.5"), ("Vodka", "l", "45"), ("Limão", "kg", "6"), ("Gelo", "kg", "2"), ("Batata", "kg", "7"), ("Isca de frango", "kg", "18")],
        "pr": [("Cerveja long neck", "Cervejas", "12", "0", [("Cerveja long neck", "1")]),
               ("Caipirinha", "Drinks", "22", "0.5", [("Vodka", "0.06"), ("Limão", "0.1"), ("Gelo", "0.1")]),
               ("Porção de batata", "Porções", "32", "1.2", [("Batata", "0.4")]),
               ("Isca de frango", "Porções", "42", "1.2", [("Isca de frango", "0.4")])]},
    "pizzaria": {"meta": "30", "ins": [("Massa de pizza", "kg", "8"), ("Muçarela", "kg", "36"), ("Molho de tomate", "kg", "9"), ("Calabresa", "kg", "24"), ("Caixa de pizza", "un", "1.2")],
        "pr": [("Pizza de muçarela", "Pizzas", "42", "0.5", [("Massa de pizza", "0.3"), ("Muçarela", "0.25"), ("Molho de tomate", "0.08"), ("Caixa de pizza", "1")]),
               ("Pizza de calabresa", "Pizzas", "46", "0.5", [("Massa de pizza", "0.3"), ("Muçarela", "0.2"), ("Molho de tomate", "0.08"), ("Calabresa", "0.15"), ("Caixa de pizza", "1")])]},
    "outro": {"meta": "30", "ins": [], "pr": []},
}

VERSION 1.0 CLASS
BEGIN
  MultiUse = -1
END
Attribute VB_Name = "Customer"
Option Explicit
Implements IPersist
Private mName As String
Public Event Saved()

Public Property Get Name() As String
    Name = mName
End Property

Public Property Let Name(ByVal value As String)
    mName = value
End Property

Public Property Set Owner(ByVal value As Object)
End Property

Public Sub Save()
    Validate
    RaiseEvent Saved
End Sub

Private Sub Validate()
End Sub

Private Sub IPersist_Save()
    Save
End Sub
